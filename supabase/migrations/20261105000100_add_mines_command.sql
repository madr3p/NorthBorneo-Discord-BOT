begin;

create schema if not exists extensions;
create extension if not exists pgcrypto with schema extensions;

alter table public.economy_profiles
    add column if not exists last_gambling_at timestamptz;

create table if not exists public.mines_games (
    game_id uuid primary key,
    start_request_id uuid not null unique,
    discord_user_id text not null,
    bet_amount bigint not null check (bet_amount between 10 and 6666666),
    mine_count integer not null check (mine_count between 1 and 24),
    mine_positions smallint[] not null,
    revealed_positions smallint[] not null default array[]::smallint[],
    safe_reveals integer not null default 0 check (safe_reveals between 0 and 24),
    game_state text not null check (game_state in ('active', 'mine_hit', 'cashed_out', 'recovered')),
    gross_payout bigint not null default 0 check (gross_payout >= 0),
    wallet_balance_after bigint not null check (wallet_balance_after >= 0),
    channel_id text not null,
    board_message_id text not null,
    cashout_message_id text not null,
    created_at timestamptz not null default pg_catalog.clock_timestamp(),
    settled_at timestamptz
);

create unique index if not exists mines_games_one_active_per_player
    on public.mines_games (discord_user_id)
    where game_state = 'active';

create table if not exists public.mines_action_requests (
    request_id uuid primary key,
    game_id uuid not null references public.mines_games(game_id),
    discord_user_id text not null,
    action_type text not null check (action_type in ('reveal', 'cashout')),
    tile smallint,
    response jsonb
);

alter table public.mines_games enable row level security;
alter table public.mines_action_requests enable row level security;
revoke all on table public.mines_games, public.mines_action_requests
    from public, anon, authenticated;
grant select, insert, update on table public.mines_games,
    public.mines_action_requests to service_role;

create or replace function public.mines_random_below(p_upper integer)
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
        raise exception 'Invalid Mines random range';
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

create or replace function public.mines_binomial(p_n integer, p_k integer)
returns numeric
language plpgsql
immutable
security invoker
set search_path = ''
as $function$
declare
    v_result numeric := 1;
    v_i integer;
    v_steps integer;
begin
    if p_n < 0 or p_k < 0 or p_k > p_n then
        return 0;
    end if;
    v_steps := case when p_k <= p_n - p_k then p_k else p_n - p_k end;
    if v_steps = 0 then
        return 1;
    end if;
    for v_i in 1..v_steps loop
        v_result := v_result * (p_n - v_i + 1)::numeric / v_i::numeric;
    end loop;
    return v_result;
end;
$function$;

create or replace function public.mines_cashout_multiplier(
    p_mine_count integer,
    p_safe_reveals integer
)
returns numeric
language sql
immutable
security invoker
set search_path = ''
as $function$
    select 99::numeric * public.mines_binomial(25, p_safe_reveals)
        / (100::numeric * public.mines_binomial(25 - p_mine_count, p_safe_reveals));
$function$;

create or replace function public.mines_cashout_amount(
    p_bet bigint,
    p_mine_count integer,
    p_safe_reveals integer
)
returns bigint
language sql
immutable
security invoker
set search_path = ''
as $function$
    select pg_catalog.floor(
        p_bet::numeric * 99::numeric * public.mines_binomial(25, p_safe_reveals)
        / (100::numeric * public.mines_binomial(25 - p_mine_count, p_safe_reveals))
    )::bigint;
$function$;

create or replace function public.mines_session_response(
    p_game_id uuid,
    p_status text
)
returns jsonb
language plpgsql
security invoker
set search_path = ''
as $function$
declare
    v_game public.mines_games%rowtype;
    v_multiplier numeric;
    v_cashout bigint;
begin
    select mg.* into v_game
    from public.mines_games as mg
    where mg.game_id = p_game_id;
    if not found then
        return pg_catalog.jsonb_build_object('game_status', 'game_not_found');
    end if;

    v_multiplier := public.mines_cashout_multiplier(v_game.mine_count, v_game.safe_reveals);
    v_cashout := public.mines_cashout_amount(v_game.bet_amount, v_game.mine_count, v_game.safe_reveals);
    return pg_catalog.jsonb_build_object(
        'game_status', p_status,
        'game_id', v_game.game_id,
        'game_state', v_game.game_state,
        'discord_user_id', v_game.discord_user_id,
        'bet_amount', v_game.bet_amount,
        'mine_count', v_game.mine_count,
        'mine_positions', case when v_game.game_state = 'active'
            then 'null'::jsonb else pg_catalog.to_jsonb(v_game.mine_positions) end,
        'revealed_positions', pg_catalog.to_jsonb(v_game.revealed_positions),
        'safe_reveals', v_game.safe_reveals,
        'current_multiplier', v_multiplier,
        'cashout_amount', case when v_game.game_state = 'active'
            then v_cashout else v_game.gross_payout end,
        'gross_payout', v_game.gross_payout,
        'wallet_balance_after', v_game.wallet_balance_after,
        'channel_id', v_game.channel_id,
        'board_message_id', v_game.board_message_id,
        'cashout_message_id', v_game.cashout_message_id
    );
end;
$function$;

create or replace function public.start_mines_game(
    p_discord_user_id text,
    p_request_id uuid,
    p_game_id uuid,
    p_bet bigint,
    p_mine_count integer,
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
    v_existing_game public.mines_games%rowtype;
    v_wallet bigint;
    v_last_gambling_at timestamptz;
    v_now timestamptz;
    v_tiles smallint[] := array[]::smallint[];
    v_mines smallint[] := array[]::smallint[];
    v_swap smallint;
    v_i integer;
    v_j integer;
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
    if p_mine_count is null or p_mine_count < 1 or p_mine_count > 24 then
        return pg_catalog.jsonb_build_object('game_status', 'invalid_mine_count');
    end if;

    select mg.* into v_existing_game
    from public.mines_games as mg
    where mg.start_request_id = p_request_id;
    if found then
        if v_existing_game.discord_user_id <> p_discord_user_id
           or v_existing_game.game_id <> p_game_id
           or v_existing_game.bet_amount <> p_bet
           or v_existing_game.mine_count <> p_mine_count then
            return pg_catalog.jsonb_build_object('game_status', 'invalid_request');
        end if;
        return public.mines_session_response(v_existing_game.game_id, 'active');
    end if;
    if exists (select 1 from public.mines_games as mg where mg.game_id = p_game_id) then
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

    -- Recheck after taking the per-player lock so a concurrent replay cannot debit twice.
    select mg.* into v_existing_game
    from public.mines_games as mg
    where mg.start_request_id = p_request_id;
    if found then
        if v_existing_game.discord_user_id <> p_discord_user_id
           or v_existing_game.game_id <> p_game_id
           or v_existing_game.bet_amount <> p_bet
           or v_existing_game.mine_count <> p_mine_count then
            return pg_catalog.jsonb_build_object('game_status', 'invalid_request');
        end if;
        return public.mines_session_response(v_existing_game.game_id, 'active');
    end if;
    if exists (
        select 1 from public.mines_games as mg
        where mg.discord_user_id = p_discord_user_id and mg.game_state = 'active'
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

    for v_i in 0..24 loop
        v_tiles := pg_catalog.array_append(v_tiles, v_i::smallint);
    end loop;
    for v_i in 0..(p_mine_count - 1) loop
        v_j := v_i + public.mines_random_below(25 - v_i);
        v_swap := v_tiles[v_i + 1];
        v_tiles[v_i + 1] := v_tiles[v_j + 1];
        v_tiles[v_j + 1] := v_swap;
        v_mines := pg_catalog.array_append(v_mines, v_tiles[v_i + 1]);
    end loop;

    update public.economy_profiles as ep
    set wallet_balance = ep.wallet_balance - p_bet,
        last_gambling_at = v_now
    where ep.discord_user_id = p_discord_user_id;

    insert into public.mines_games (
        game_id, start_request_id, discord_user_id, bet_amount, mine_count,
        mine_positions, revealed_positions, safe_reveals, game_state,
        gross_payout, wallet_balance_after, channel_id, board_message_id,
        cashout_message_id
    ) values (
        p_game_id, p_request_id, p_discord_user_id, p_bet, p_mine_count,
        v_mines, array[]::smallint[], 0, 'active', 0,
        v_wallet - p_bet, p_channel_id, p_board_message_id, p_cashout_message_id
    );
    return public.mines_session_response(p_game_id, 'active');
end;
$function$;

create or replace function public.reveal_mines_tile(
    p_game_id uuid,
    p_discord_user_id text,
    p_request_id uuid,
    p_tile integer
)
returns jsonb
language plpgsql
security invoker
set search_path = ''
as $function$
declare
    v_game public.mines_games%rowtype;
    v_old_game_id uuid;
    v_old_user_id text;
    v_old_action text;
    v_old_tile smallint;
    v_cached jsonb;
    v_response jsonb;
    v_tile smallint;
    v_safe_count integer;
    v_payout bigint;
begin
    if p_game_id is null or p_request_id is null
       or p_discord_user_id is null or pg_catalog.btrim(p_discord_user_id) = '' then
        return pg_catalog.jsonb_build_object('game_status', 'invalid_request');
    end if;
    if p_tile is null or p_tile < 0 or p_tile >= 25 then
        return pg_catalog.jsonb_build_object('game_status', 'invalid_tile');
    end if;

    select mg.* into v_game
    from public.mines_games as mg
    where mg.game_id = p_game_id
    for update;
    if not found then
        return pg_catalog.jsonb_build_object('game_status', 'game_not_found');
    end if;
    if v_game.discord_user_id <> p_discord_user_id then
        return pg_catalog.jsonb_build_object('game_status', 'not_owner');
    end if;

    insert into public.mines_action_requests (request_id, game_id, discord_user_id, action_type, tile)
    values (p_request_id, p_game_id, p_discord_user_id, 'reveal', p_tile::smallint)
    on conflict (request_id) do nothing;
    if not found then
        select ar.game_id, ar.discord_user_id, ar.action_type, ar.tile, ar.response
        into v_old_game_id, v_old_user_id, v_old_action, v_old_tile, v_cached
        from public.mines_action_requests as ar
        where ar.request_id = p_request_id;
        if v_old_game_id <> p_game_id or v_old_user_id <> p_discord_user_id
           or v_old_action <> 'reveal' or v_old_tile <> p_tile::smallint then
            return pg_catalog.jsonb_build_object('game_status', 'invalid_request');
        end if;
        if v_cached is not null then
            if v_game.game_state <> 'active' then
                return public.mines_session_response(p_game_id, 'already_ended');
            elsif pg_catalog.array_position(v_game.revealed_positions, p_tile::smallint) is not null then
                return public.mines_session_response(p_game_id, 'tile_already_revealed');
            end if;
            return v_cached;
        end if;
        raise exception 'Mines action result is unavailable';
    end if;

    if v_game.game_state <> 'active' then
        v_response := public.mines_session_response(p_game_id, 'already_ended');
    elsif pg_catalog.array_position(v_game.revealed_positions, p_tile::smallint) is not null then
        v_response := public.mines_session_response(p_game_id, 'tile_already_revealed');
    else
        v_tile := p_tile::smallint;
        v_game.revealed_positions := pg_catalog.array_append(v_game.revealed_positions, v_tile);
        if pg_catalog.array_position(v_game.mine_positions, v_tile) is not null then
            update public.mines_games as mg
            set revealed_positions = v_game.revealed_positions,
                game_state = 'mine_hit',
                gross_payout = 0,
                settled_at = pg_catalog.clock_timestamp()
            where mg.game_id = p_game_id;
            v_response := public.mines_session_response(p_game_id, 'mine_hit');
        else
            v_safe_count := v_game.safe_reveals + 1;
            v_payout := public.mines_cashout_amount(v_game.bet_amount, v_game.mine_count, v_safe_count);
            update public.mines_games as mg
            set revealed_positions = v_game.revealed_positions,
                safe_reveals = v_safe_count
            where mg.game_id = p_game_id;
            v_response := public.mines_session_response(p_game_id, 'active');
        end if;
    end if;

    update public.mines_action_requests as ar
    set response = v_response
    where ar.request_id = p_request_id;
    return v_response;
end;
$function$;

create or replace function public.cashout_mines_game(
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
    v_game public.mines_games%rowtype;
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

    select mg.* into v_game
    from public.mines_games as mg
    where mg.game_id = p_game_id
    for update;
    if not found then
        return pg_catalog.jsonb_build_object('game_status', 'game_not_found');
    end if;
    if v_game.discord_user_id <> p_discord_user_id then
        return pg_catalog.jsonb_build_object('game_status', 'not_owner');
    end if;

    insert into public.mines_action_requests (request_id, game_id, discord_user_id, action_type, tile)
    values (p_request_id, p_game_id, p_discord_user_id, 'cashout', null)
    on conflict (request_id) do nothing;
    if not found then
        select ar.game_id, ar.discord_user_id, ar.action_type, ar.response
        into v_old_game_id, v_old_user_id, v_old_action, v_cached
        from public.mines_action_requests as ar
        where ar.request_id = p_request_id;
        if v_old_game_id <> p_game_id or v_old_user_id <> p_discord_user_id
           or v_old_action <> 'cashout' then
            return pg_catalog.jsonb_build_object('game_status', 'invalid_request');
        end if;
        if v_cached is not null then
            if v_game.game_state <> 'active' then
                return public.mines_session_response(p_game_id, 'already_ended');
            end if;
            if (v_cached ->> 'game_status') = 'balance_limit_reached' then
                return public.mines_session_response(p_game_id, 'balance_limit_reached');
            end if;
            if (v_cached ->> 'game_status') = 'no_safe_tiles' then
                return public.mines_session_response(p_game_id, 'no_safe_tiles');
            end if;
            return v_cached;
        end if;
        raise exception 'Mines cashout result is unavailable';
    end if;

    if v_game.game_state <> 'active' then
        v_response := public.mines_session_response(p_game_id, 'already_ended');
    elsif v_game.safe_reveals < 1 then
        v_response := public.mines_session_response(p_game_id, 'no_safe_tiles');
    else
        select ep.wallet_balance into v_wallet
        from public.economy_profiles as ep
        where ep.discord_user_id = p_discord_user_id
        for update;
        if not found then
            v_response := pg_catalog.jsonb_build_object('game_status', 'profile_not_found');
        else
            v_payout := public.mines_cashout_amount(v_game.bet_amount, v_game.mine_count, v_game.safe_reveals);
            if v_wallet::numeric + v_payout::numeric > 9223372036854775807::numeric then
                v_response := public.mines_session_response(p_game_id, 'balance_limit_reached');
            else
                v_wallet := v_wallet + v_payout;
                update public.economy_profiles as ep
                set wallet_balance = v_wallet
                where ep.discord_user_id = p_discord_user_id;
                update public.mines_games as mg
                set game_state = 'cashed_out',
                    gross_payout = v_payout,
                    wallet_balance_after = v_wallet,
                    settled_at = pg_catalog.clock_timestamp()
                where mg.game_id = p_game_id;
                v_response := public.mines_session_response(p_game_id, 'cashed_out');
            end if;
        end if;
    end if;

    update public.mines_action_requests as ar
    set response = v_response
    where ar.request_id = p_request_id;
    return v_response;
end;
$function$;

create or replace function public.recover_mines_game(
    p_game_id uuid,
    p_discord_user_id text
)
returns jsonb
language plpgsql
security invoker
set search_path = ''
as $function$
declare
    v_game public.mines_games%rowtype;
    v_wallet bigint;
    v_amount bigint;
    v_status text;
begin
    if p_game_id is null or p_discord_user_id is null or pg_catalog.btrim(p_discord_user_id) = '' then
        return pg_catalog.jsonb_build_object('game_status', 'invalid_request');
    end if;
    select mg.* into v_game
    from public.mines_games as mg
    where mg.game_id = p_game_id
    for update;
    if not found then
        return pg_catalog.jsonb_build_object('game_status', 'game_not_found');
    end if;
    if v_game.discord_user_id <> p_discord_user_id then
        return pg_catalog.jsonb_build_object('game_status', 'not_owner');
    end if;
    if v_game.game_state <> 'active' then
        return public.mines_session_response(p_game_id, 'already_ended');
    end if;

    select ep.wallet_balance into v_wallet
    from public.economy_profiles as ep
    where ep.discord_user_id = p_discord_user_id
    for update;
    if not found then
        return pg_catalog.jsonb_build_object('game_status', 'profile_not_found');
    end if;

    if v_game.safe_reveals > 0 then
        v_amount := public.mines_cashout_amount(v_game.bet_amount, v_game.mine_count, v_game.safe_reveals);
        v_status := 'recovered';
    else
        v_amount := v_game.bet_amount;
        v_status := 'recovered';
    end if;

    if v_wallet::numeric + v_amount::numeric > 9223372036854775807::numeric then
        return public.mines_session_response(p_game_id, 'balance_limit_reached');
    end if;
    v_wallet := v_wallet + v_amount;
    update public.economy_profiles as ep
    set wallet_balance = v_wallet
    where ep.discord_user_id = p_discord_user_id;
    update public.mines_games as mg
    set game_state = 'recovered',
        gross_payout = v_amount,
        wallet_balance_after = v_wallet,
        settled_at = pg_catalog.clock_timestamp()
    where mg.game_id = p_game_id;
    return public.mines_session_response(p_game_id, v_status);
end;
$function$;

create or replace function public.list_active_mines_games()
returns jsonb
language sql
security invoker
set search_path = ''
as $function$
    select coalesce(
        pg_catalog.jsonb_agg(public.mines_session_response(mg.game_id, 'active')),
        '[]'::jsonb
    )
    from public.mines_games as mg
    where mg.game_state = 'active';
$function$;

revoke all on function public.mines_random_below(integer)
    from public, anon, authenticated;
revoke all on function public.mines_binomial(integer, integer)
    from public, anon, authenticated;
revoke all on function public.mines_cashout_multiplier(integer, integer)
    from public, anon, authenticated;
revoke all on function public.mines_cashout_amount(bigint, integer, integer)
    from public, anon, authenticated;
revoke all on function public.mines_session_response(uuid, text)
    from public, anon, authenticated;
revoke all on function public.start_mines_game(text, uuid, uuid, bigint, integer, text, text, text)
    from public, anon, authenticated;
revoke all on function public.reveal_mines_tile(uuid, text, uuid, integer)
    from public, anon, authenticated;
revoke all on function public.cashout_mines_game(uuid, text, uuid)
    from public, anon, authenticated;
revoke all on function public.recover_mines_game(uuid, text)
    from public, anon, authenticated;
revoke all on function public.list_active_mines_games()
    from public, anon, authenticated;

grant execute on function public.mines_random_below(integer) to service_role;
grant execute on function public.mines_binomial(integer, integer) to service_role;
grant execute on function public.mines_cashout_multiplier(integer, integer) to service_role;
grant execute on function public.mines_cashout_amount(bigint, integer, integer) to service_role;
grant execute on function public.mines_session_response(uuid, text) to service_role;
grant execute on function public.start_mines_game(text, uuid, uuid, bigint, integer, text, text, text) to service_role;
grant execute on function public.reveal_mines_tile(uuid, text, uuid, integer) to service_role;
grant execute on function public.cashout_mines_game(uuid, text, uuid) to service_role;
grant execute on function public.recover_mines_game(uuid, text) to service_role;
grant execute on function public.list_active_mines_games() to service_role;

commit;
