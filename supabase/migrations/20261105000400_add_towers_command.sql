begin;

create schema if not exists extensions;
create extension if not exists pgcrypto with schema extensions;

create table if not exists public.towers_games (
    game_id uuid primary key,
    start_request_id uuid not null unique,
    discord_user_id text not null,
    bet_amount bigint not null check (bet_amount between 10 and 6666666),
    trap_positions smallint[] not null
        check (pg_catalog.cardinality(trap_positions) = 8)
        check (trap_positions <@ array[0::smallint, 1::smallint, 2::smallint]),
    selected_tiles smallint[] not null default array[]::smallint[]
        check (pg_catalog.cardinality(selected_tiles) <= 8)
        check (selected_tiles <@ array[0::smallint, 1::smallint, 2::smallint]),
    safe_picks integer not null default 0 check (safe_picks between 0 and 8),
    game_state text not null check (game_state in ('active', 'lost', 'cashed_out', 'completed', 'recovered')),
    trap_hit_row smallint check (trap_hit_row between 1 and 8),
    gross_payout bigint not null default 0 check (gross_payout >= 0),
    wallet_balance_after bigint not null check (wallet_balance_after >= 0),
    channel_id text not null,
    board_message_id text not null,
    cashout_message_id text not null,
    created_at timestamptz not null default pg_catalog.clock_timestamp(),
    settled_at timestamptz
);

create unique index if not exists towers_games_one_active_per_player
    on public.towers_games (discord_user_id)
    where game_state = 'active';

create table if not exists public.towers_action_requests (
    request_id uuid primary key,
    game_id uuid not null references public.towers_games(game_id),
    discord_user_id text not null,
    action_type text not null check (action_type in ('pick', 'cashout')),
    tile smallint,
    expected_row smallint,
    response jsonb
);

alter table public.towers_games enable row level security;
alter table public.towers_action_requests enable row level security;
revoke all on table public.towers_games, public.towers_action_requests
    from public, anon, authenticated;
grant select, insert, update on table public.towers_games,
    public.towers_action_requests to service_role;

create or replace function public.towers_random_below(p_upper integer)
returns integer
language plpgsql
security invoker
set search_path = ''
as $function$
declare
    v_bytes bytea;
    v_value bigint;
    v_limit bigint;
begin
    if p_upper is null or p_upper <= 0 then
        raise exception 'Invalid Towers random range';
    end if;
    v_limit := (4294967296::bigint / p_upper::bigint) * p_upper::bigint;
    loop
        v_bytes := extensions.gen_random_bytes(4);
        v_value :=
            (pg_catalog.get_byte(v_bytes, 0)::bigint << 24)
            + (pg_catalog.get_byte(v_bytes, 1)::bigint << 16)
            + (pg_catalog.get_byte(v_bytes, 2)::bigint << 8)
            + pg_catalog.get_byte(v_bytes, 3)::bigint;
        exit when v_value < v_limit;
    end loop;
    return (v_value % p_upper)::integer;
end;
$function$;

-- 0.99 * (3/2)^K, computed as an exact rational decimal.
create or replace function public.towers_multiplier(p_safe_picks integer)
returns numeric
language plpgsql
immutable
security invoker
set search_path = ''
as $function$
declare
    v_numerator numeric := 99;
    v_denominator numeric := 100;
    v_step integer;
begin
    if p_safe_picks is null or p_safe_picks < 0 or p_safe_picks > 8 then
        raise exception 'Invalid Towers progress';
    end if;
    for v_step in 1..p_safe_picks loop
        v_numerator := v_numerator * 3;
        v_denominator := v_denominator * 2;
    end loop;
    return v_numerator / v_denominator;
end;
$function$;

create or replace function public.towers_cashout_amount(
    p_bet bigint,
    p_safe_picks integer
)
returns bigint
language sql
immutable
security invoker
set search_path = ''
as $function$
    select pg_catalog.floor(p_bet::numeric * public.towers_multiplier(p_safe_picks))::bigint;
$function$;

create or replace function public.towers_session_response(
    p_game_id uuid,
    p_status text
)
returns jsonb
language plpgsql
security invoker
set search_path = ''
as $function$
declare
    v_game public.towers_games%rowtype;
    v_multiplier numeric;
    v_cashout bigint;
begin
    select tg.* into v_game
    from public.towers_games as tg
    where tg.game_id = p_game_id;
    if not found then
        return pg_catalog.jsonb_build_object('game_status', 'game_not_found');
    end if;

    v_multiplier := public.towers_multiplier(v_game.safe_picks);
    v_cashout := public.towers_cashout_amount(v_game.bet_amount, v_game.safe_picks);
    return pg_catalog.jsonb_build_object(
        'game_status', p_status,
        'game_id', v_game.game_id,
        'game_state', v_game.game_state,
        'discord_user_id', v_game.discord_user_id,
        'bet_amount', v_game.bet_amount,
        'trap_positions', case when v_game.game_state = 'active'
            then 'null'::jsonb else pg_catalog.to_jsonb(v_game.trap_positions) end,
        'selected_tiles', pg_catalog.to_jsonb(v_game.selected_tiles),
        'safe_picks', v_game.safe_picks,
        'current_multiplier', v_multiplier,
        'cashout_amount', case when v_game.game_state = 'active'
            then v_cashout else v_game.gross_payout end,
        'gross_payout', v_game.gross_payout,
        'wallet_balance_after', v_game.wallet_balance_after,
        'trap_hit_row', v_game.trap_hit_row,
        'channel_id', v_game.channel_id,
        'board_message_id', v_game.board_message_id,
        'cashout_message_id', v_game.cashout_message_id
    );
end;
$function$;

create or replace function public.start_towers_game(
    p_discord_user_id text,
    p_request_id uuid,
    p_game_id uuid,
    p_bet bigint,
    p_channel_id text,
    p_board_message_id text,
    p_cashout_message_id text
)
returns jsonb
language plpgsql
security invoker
set search_path = ''
as $function$
declare
    v_existing public.towers_games%rowtype;
    v_wallet bigint;
    v_last_gambling_at timestamptz;
    v_now timestamptz;
    v_traps smallint[] := array[]::smallint[];
    v_row integer;
begin
    if p_discord_user_id is null or pg_catalog.btrim(p_discord_user_id) = ''
       or p_request_id is null or p_game_id is null
       or p_channel_id is null or p_board_message_id is null or p_cashout_message_id is null
       or p_channel_id = '' or p_board_message_id = '' or p_cashout_message_id = '' then
        return pg_catalog.jsonb_build_object('game_status', 'invalid_request');
    end if;
    if p_bet is null or p_bet < 10 or p_bet > 6666666 then
        return pg_catalog.jsonb_build_object('game_status', 'invalid_bet');
    end if;

    select tg.* into v_existing
    from public.towers_games as tg
    where tg.start_request_id = p_request_id;
    if found then
        if v_existing.discord_user_id <> p_discord_user_id
           or v_existing.game_id <> p_game_id
           or v_existing.bet_amount <> p_bet
           or v_existing.channel_id <> p_channel_id
           or v_existing.board_message_id <> p_board_message_id
           or v_existing.cashout_message_id <> p_cashout_message_id then
            return pg_catalog.jsonb_build_object('game_status', 'invalid_request');
        end if;
        return public.towers_session_response(v_existing.game_id, 'active');
    end if;
    if exists (select 1 from public.towers_games as tg where tg.game_id = p_game_id) then
        return pg_catalog.jsonb_build_object('game_status', 'invalid_request');
    end if;

    select ep.wallet_balance, ep.last_gambling_at
    into v_wallet, v_last_gambling_at
    from public.economy_profiles as ep
    where ep.discord_user_id = p_discord_user_id
    for update;
    if not found then
        return pg_catalog.jsonb_build_object('game_status', 'profile_not_found');
    end if;

    -- Recheck after locking the profile so a concurrent replay cannot debit twice.
    select tg.* into v_existing
    from public.towers_games as tg
    where tg.start_request_id = p_request_id;
    if found then
        if v_existing.discord_user_id <> p_discord_user_id
           or v_existing.game_id <> p_game_id
           or v_existing.bet_amount <> p_bet
           or v_existing.channel_id <> p_channel_id
           or v_existing.board_message_id <> p_board_message_id
           or v_existing.cashout_message_id <> p_cashout_message_id then
            return pg_catalog.jsonb_build_object('game_status', 'invalid_request');
        end if;
        return public.towers_session_response(v_existing.game_id, 'active');
    end if;
    if exists (
        select 1 from public.towers_games as tg
        where tg.discord_user_id = p_discord_user_id and tg.game_state = 'active'
    ) then
        return pg_catalog.jsonb_build_object('game_status', 'active_game_exists');
    end if;

    v_now := pg_catalog.clock_timestamp();
    if v_last_gambling_at is not null
       and v_last_gambling_at + interval '5 seconds' > v_now then
        return pg_catalog.jsonb_build_object(
            'game_status', 'cooldown',
            'wallet_balance_after', v_wallet,
            'next_gambling_at', v_last_gambling_at + interval '5 seconds'
        );
    end if;
    if v_wallet < p_bet then
        return pg_catalog.jsonb_build_object(
            'game_status', 'insufficient_funds',
            'wallet_balance_after', v_wallet
        );
    end if;

    for v_row in 1..8 loop
        v_traps := pg_catalog.array_append(v_traps, public.towers_random_below(3)::smallint);
    end loop;

    update public.economy_profiles as ep
    set wallet_balance = ep.wallet_balance - p_bet,
        last_gambling_at = v_now
    where ep.discord_user_id = p_discord_user_id;

    insert into public.towers_games (
        game_id, start_request_id, discord_user_id, bet_amount, trap_positions,
        selected_tiles, safe_picks, game_state, trap_hit_row, gross_payout,
        wallet_balance_after, channel_id, board_message_id, cashout_message_id
    ) values (
        p_game_id, p_request_id, p_discord_user_id, p_bet, v_traps,
        array[]::smallint[], 0, 'active', null, 0,
        v_wallet - p_bet, p_channel_id, p_board_message_id, p_cashout_message_id
    );
    return public.towers_session_response(p_game_id, 'active');
end;
$function$;

create or replace function public.pick_towers_tile(
    p_game_id uuid,
    p_discord_user_id text,
    p_request_id uuid,
    p_tile integer,
    p_expected_row integer
)
returns jsonb
language plpgsql
security invoker
set search_path = ''
as $function$
declare
    v_game public.towers_games%rowtype;
    v_old_game_id uuid;
    v_old_user_id text;
    v_old_action text;
    v_old_tile smallint;
    v_old_row smallint;
    v_cached jsonb;
    v_response jsonb;
    v_wallet bigint;
    v_candidate numeric;
    v_cashout bigint;
    v_new_picks integer;
    v_now timestamptz;
begin
    if p_game_id is null or p_request_id is null
       or p_discord_user_id is null or pg_catalog.btrim(p_discord_user_id) = '' then
        return pg_catalog.jsonb_build_object('game_status', 'invalid_request');
    end if;
    if p_tile is null or p_tile < 0 or p_tile >= 3
       or p_expected_row is null or p_expected_row < 0 or p_expected_row >= 8 then
        return pg_catalog.jsonb_build_object('game_status', 'invalid_tile');
    end if;

    select tg.* into v_game
    from public.towers_games as tg
    where tg.game_id = p_game_id
    for update;
    if not found then
        return pg_catalog.jsonb_build_object('game_status', 'game_not_found');
    end if;
    if v_game.discord_user_id <> p_discord_user_id then
        return pg_catalog.jsonb_build_object('game_status', 'not_owner');
    end if;

    insert into public.towers_action_requests (
        request_id, game_id, discord_user_id, action_type, tile, expected_row
    ) values (
        p_request_id, p_game_id, p_discord_user_id, 'pick', p_tile::smallint, p_expected_row::smallint
    ) on conflict (request_id) do nothing;
    if not found then
        select ar.game_id, ar.discord_user_id, ar.action_type, ar.tile, ar.expected_row, ar.response
        into v_old_game_id, v_old_user_id, v_old_action, v_old_tile, v_old_row, v_cached
        from public.towers_action_requests as ar
        where ar.request_id = p_request_id;
        if v_old_game_id <> p_game_id or v_old_user_id <> p_discord_user_id
           or v_old_action <> 'pick' or v_old_tile <> p_tile::smallint
           or v_old_row <> p_expected_row::smallint then
            return pg_catalog.jsonb_build_object('game_status', 'invalid_request');
        end if;
        if v_game.game_state <> 'active' then
            return public.towers_session_response(p_game_id, 'already_ended');
        elsif v_game.safe_picks <> p_expected_row then
            return public.towers_session_response(p_game_id, 'row_changed');
        elsif v_cached is not null then
            return v_cached;
        end if;
        raise exception 'Towers action result is unavailable';
    end if;

    if v_game.game_state <> 'active' then
        v_response := public.towers_session_response(p_game_id, 'already_ended');
    elsif v_game.safe_picks <> p_expected_row then
        v_response := public.towers_session_response(p_game_id, 'row_changed');
    elsif v_game.safe_picks >= 8 then
        v_response := public.towers_session_response(p_game_id, 'row_complete');
    elsif v_game.trap_positions[v_game.safe_picks + 1] = p_tile::smallint then
        update public.towers_games as tg
        set selected_tiles = pg_catalog.array_append(v_game.selected_tiles, p_tile::smallint),
            game_state = 'lost',
            trap_hit_row = (v_game.safe_picks + 1)::smallint,
            gross_payout = 0,
            settled_at = pg_catalog.clock_timestamp()
        where tg.game_id = p_game_id;
        v_response := public.towers_session_response(p_game_id, 'trap_hit');
    else
        v_new_picks := v_game.safe_picks + 1;
        update public.towers_games as tg
        set selected_tiles = pg_catalog.array_append(v_game.selected_tiles, p_tile::smallint),
            safe_picks = v_new_picks
        where tg.game_id = p_game_id;

        if v_new_picks = 8 then
            select ep.wallet_balance into v_wallet
            from public.economy_profiles as ep
            where ep.discord_user_id = p_discord_user_id
            for update;
            if not found then
                v_response := pg_catalog.jsonb_build_object('game_status', 'profile_not_found');
            else
                v_cashout := public.towers_cashout_amount(v_game.bet_amount, v_new_picks);
                v_candidate := v_wallet::numeric + v_cashout::numeric;
                if v_candidate > 9223372036854775807::numeric then
                    -- Keep the final floor active so a user may cash out after
                    -- available wallet capacity is restored.
                    v_response := public.towers_session_response(p_game_id, 'balance_limit_reached');
                else
                    v_wallet := v_candidate::bigint;
                    v_now := pg_catalog.clock_timestamp();
                    update public.economy_profiles as ep
                    set wallet_balance = v_wallet
                    where ep.discord_user_id = p_discord_user_id;
                    update public.towers_games as tg
                    set game_state = 'completed',
                        gross_payout = v_cashout,
                        wallet_balance_after = v_wallet,
                        settled_at = v_now
                    where tg.game_id = p_game_id;
                    v_response := public.towers_session_response(p_game_id, 'completed');
                end if;
            end if;
        else
            v_response := public.towers_session_response(p_game_id, 'active');
        end if;
    end if;

    update public.towers_action_requests as ar
    set response = v_response
    where ar.request_id = p_request_id;
    return v_response;
end;
$function$;

create or replace function public.cashout_towers_game(
    p_game_id uuid,
    p_discord_user_id text,
    p_request_id uuid
)
returns jsonb
language plpgsql
security invoker
set search_path = ''
as $function$
declare
    v_game public.towers_games%rowtype;
    v_old_game_id uuid;
    v_old_user_id text;
    v_old_action text;
    v_cached jsonb;
    v_response jsonb;
    v_wallet bigint;
    v_payout bigint;
begin
    if p_game_id is null or p_request_id is null
       or p_discord_user_id is null or pg_catalog.btrim(p_discord_user_id) = '' then
        return pg_catalog.jsonb_build_object('game_status', 'invalid_request');
    end if;
    select tg.* into v_game
    from public.towers_games as tg
    where tg.game_id = p_game_id
    for update;
    if not found then
        return pg_catalog.jsonb_build_object('game_status', 'game_not_found');
    end if;
    if v_game.discord_user_id <> p_discord_user_id then
        return pg_catalog.jsonb_build_object('game_status', 'not_owner');
    end if;

    insert into public.towers_action_requests (request_id, game_id, discord_user_id, action_type)
    values (p_request_id, p_game_id, p_discord_user_id, 'cashout')
    on conflict (request_id) do nothing;
    if not found then
        select ar.game_id, ar.discord_user_id, ar.action_type, ar.response
        into v_old_game_id, v_old_user_id, v_old_action, v_cached
        from public.towers_action_requests as ar
        where ar.request_id = p_request_id;
        if v_old_game_id <> p_game_id or v_old_user_id <> p_discord_user_id
           or v_old_action <> 'cashout' then
            return pg_catalog.jsonb_build_object('game_status', 'invalid_request');
        end if;
        if v_game.game_state <> 'active' then
            return public.towers_session_response(p_game_id, 'already_ended');
        elsif v_cached is not null and (v_cached ->> 'game_status') = 'balance_limit_reached' then
            return public.towers_session_response(p_game_id, 'balance_limit_reached');
        elsif v_cached is not null and (v_cached ->> 'game_status') = 'no_safe_picks' then
            return public.towers_session_response(p_game_id, 'no_safe_picks');
        elsif v_cached is not null then
            return v_cached;
        end if;
        raise exception 'Towers cashout result is unavailable';
    end if;

    if v_game.game_state <> 'active' then
        v_response := public.towers_session_response(p_game_id, 'already_ended');
    elsif v_game.safe_picks < 1 then
        v_response := public.towers_session_response(p_game_id, 'no_safe_picks');
    else
        select ep.wallet_balance into v_wallet
        from public.economy_profiles as ep
        where ep.discord_user_id = p_discord_user_id
        for update;
        if not found then
            v_response := pg_catalog.jsonb_build_object('game_status', 'profile_not_found');
        else
            v_payout := public.towers_cashout_amount(v_game.bet_amount, v_game.safe_picks);
            if v_wallet::numeric + v_payout::numeric > 9223372036854775807::numeric then
                v_response := public.towers_session_response(p_game_id, 'balance_limit_reached');
            else
                v_wallet := v_wallet + v_payout;
                update public.economy_profiles as ep
                set wallet_balance = v_wallet
                where ep.discord_user_id = p_discord_user_id;
                update public.towers_games as tg
                set game_state = 'cashed_out',
                    gross_payout = v_payout,
                    wallet_balance_after = v_wallet,
                    settled_at = pg_catalog.clock_timestamp()
                where tg.game_id = p_game_id;
                v_response := public.towers_session_response(p_game_id, 'cashed_out');
            end if;
        end if;
    end if;

    update public.towers_action_requests as ar
    set response = v_response
    where ar.request_id = p_request_id;
    return v_response;
end;
$function$;

create or replace function public.recover_towers_game(
    p_game_id uuid,
    p_discord_user_id text
)
returns jsonb
language plpgsql
security invoker
set search_path = ''
as $function$
declare
    v_game public.towers_games%rowtype;
    v_wallet bigint;
    v_amount bigint;
begin
    if p_game_id is null or p_discord_user_id is null or pg_catalog.btrim(p_discord_user_id) = '' then
        return pg_catalog.jsonb_build_object('game_status', 'invalid_request');
    end if;
    select tg.* into v_game
    from public.towers_games as tg
    where tg.game_id = p_game_id
    for update;
    if not found then
        return pg_catalog.jsonb_build_object('game_status', 'game_not_found');
    end if;
    if v_game.discord_user_id <> p_discord_user_id then
        return pg_catalog.jsonb_build_object('game_status', 'not_owner');
    end if;
    if v_game.game_state <> 'active' then
        return public.towers_session_response(p_game_id, 'already_ended');
    end if;

    select ep.wallet_balance into v_wallet
    from public.economy_profiles as ep
    where ep.discord_user_id = p_discord_user_id
    for update;
    if not found then
        return pg_catalog.jsonb_build_object('game_status', 'profile_not_found');
    end if;

    if v_game.safe_picks > 0 then
        v_amount := public.towers_cashout_amount(v_game.bet_amount, v_game.safe_picks);
    else
        v_amount := v_game.bet_amount;
    end if;
    if v_wallet::numeric + v_amount::numeric > 9223372036854775807::numeric then
        return public.towers_session_response(p_game_id, 'balance_limit_reached');
    end if;

    v_wallet := v_wallet + v_amount;
    update public.economy_profiles as ep
    set wallet_balance = v_wallet
    where ep.discord_user_id = p_discord_user_id;
    update public.towers_games as tg
    set game_state = 'recovered',
        gross_payout = v_amount,
        wallet_balance_after = v_wallet,
        settled_at = pg_catalog.clock_timestamp()
    where tg.game_id = p_game_id;
    return public.towers_session_response(p_game_id, 'recovered');
end;
$function$;

create or replace function public.list_active_towers_games()
returns jsonb
language sql
security invoker
set search_path = ''
as $function$
    select coalesce(
        pg_catalog.jsonb_agg(public.towers_session_response(tg.game_id, 'active')),
        '[]'::jsonb
    )
    from public.towers_games as tg
    where tg.game_state = 'active';
$function$;

revoke all on function public.towers_random_below(integer)
    from public, anon, authenticated;
revoke all on function public.towers_multiplier(integer)
    from public, anon, authenticated;
revoke all on function public.towers_cashout_amount(bigint, integer)
    from public, anon, authenticated;
revoke all on function public.towers_session_response(uuid, text)
    from public, anon, authenticated;
revoke all on function public.start_towers_game(text, uuid, uuid, bigint, text, text, text)
    from public, anon, authenticated;
revoke all on function public.pick_towers_tile(uuid, text, uuid, integer, integer)
    from public, anon, authenticated;
revoke all on function public.cashout_towers_game(uuid, text, uuid)
    from public, anon, authenticated;
revoke all on function public.recover_towers_game(uuid, text)
    from public, anon, authenticated;
revoke all on function public.list_active_towers_games()
    from public, anon, authenticated;

grant execute on function public.towers_random_below(integer) to service_role;
grant execute on function public.towers_multiplier(integer) to service_role;
grant execute on function public.towers_cashout_amount(bigint, integer) to service_role;
grant execute on function public.towers_session_response(uuid, text) to service_role;
grant execute on function public.start_towers_game(text, uuid, uuid, bigint, text, text, text) to service_role;
grant execute on function public.pick_towers_tile(uuid, text, uuid, integer, integer) to service_role;
grant execute on function public.cashout_towers_game(uuid, text, uuid) to service_role;
grant execute on function public.recover_towers_game(uuid, text) to service_role;
grant execute on function public.list_active_towers_games() to service_role;

commit;
