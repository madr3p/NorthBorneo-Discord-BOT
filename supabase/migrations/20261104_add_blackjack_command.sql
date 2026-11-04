begin;

create schema if not exists extensions;
create extension if not exists pgcrypto with schema extensions;

alter table public.economy_profiles
    add column if not exists last_gambling_at timestamptz;

create table if not exists public.blackjack_sessions (
    game_id uuid primary key,
    start_request_id uuid not null unique,
    discord_user_id text not null,
    bet_amount bigint not null check (bet_amount between 10 and 6666666),
    deck text[] not null,
    next_card_index integer not null,
    player_cards text[] not null,
    dealer_cards text[] not null,
    game_state text not null check (game_state in ('active', 'settling', 'settled')),
    finish_reason text,
    settlement_result text,
    gross_payout bigint not null default 0 check (gross_payout >= 0),
    wallet_balance_after bigint not null check (wallet_balance_after >= 0),
    next_gambling_at timestamptz not null,
    created_at timestamptz not null default pg_catalog.clock_timestamp(),
    settled_at timestamptz
);

create table if not exists public.blackjack_action_requests (
    action_id text primary key,
    game_id uuid not null,
    discord_user_id text not null,
    response jsonb
);

alter table public.blackjack_sessions enable row level security;
alter table public.blackjack_action_requests enable row level security;
revoke all on table public.blackjack_sessions, public.blackjack_action_requests
    from public, anon, authenticated;
grant select, insert, update on table public.blackjack_sessions,
    public.blackjack_action_requests to service_role;

create or replace function public.blackjack_random_below(p_upper integer)
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
        raise exception 'Invalid random range';
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

create or replace function public.blackjack_hand_value(p_cards text[])
returns integer
language plpgsql
security invoker
set search_path = ''
as $function$
declare
    v_card text;
    v_rank text;
    v_total integer := 0;
    v_has_ace boolean := false;
begin
    foreach v_card in array coalesce(p_cards, array[]::text[]) loop
        v_rank := pg_catalog.left(v_card, pg_catalog.length(v_card) - 1);
        if v_rank = 'A' then
            v_total := v_total + 1;
            v_has_ace := true;
        elsif v_rank in ('J', 'Q', 'K') then
            v_total := v_total + 10;
        else
            v_total := v_total + v_rank::integer;
        end if;
    end loop;

    if v_has_ace and v_total <= 11 then
        v_total := v_total + 10;
    end if;
    return v_total;
end;
$function$;

create or replace function public.blackjack_session_response(
    p_game_id uuid,
    p_status text
)
returns jsonb
language plpgsql
security invoker
set search_path = ''
as $function$
declare
    v_session public.blackjack_sessions%rowtype;
    v_player_score integer;
    v_dealer_score integer;
begin
    select bs.*
    into v_session
    from public.blackjack_sessions as bs
    where bs.game_id = p_game_id;

    if not found then
        return pg_catalog.jsonb_build_object('game_status', 'game_not_found');
    end if;

    v_player_score := public.blackjack_hand_value(v_session.player_cards);
    if v_session.game_state = 'settled' then
        v_dealer_score := public.blackjack_hand_value(v_session.dealer_cards);
    else
        v_dealer_score := public.blackjack_hand_value(
            array[v_session.dealer_cards[1]]::text[]
        );
    end if;

    return pg_catalog.jsonb_build_object(
        'game_status', p_status,
        'game_id', v_session.game_id,
        'game_state', v_session.game_state,
        'game_result', v_session.settlement_result,
        'finish_reason', v_session.finish_reason,
        'bet_amount', v_session.bet_amount,
        'player_cards', v_session.player_cards,
        'dealer_visible_card', v_session.dealer_cards[1],
        'dealer_cards', case when v_session.game_state = 'settled'
            then pg_catalog.to_jsonb(v_session.dealer_cards) else 'null'::jsonb end,
        'player_score', v_player_score,
        'dealer_score', v_dealer_score,
        'gross_payout', v_session.gross_payout,
        'wallet_balance_after', v_session.wallet_balance_after,
        'next_gambling_at', v_session.next_gambling_at
    );
end;
$function$;

create or replace function public.settle_blackjack_game(p_game_id uuid)
returns jsonb
language plpgsql
security invoker
set search_path = ''
as $function$
declare
    v_session public.blackjack_sessions%rowtype;
    v_wallet_balance bigint;
    v_player_score integer;
    v_dealer_score integer;
    v_player_natural boolean;
    v_dealer_natural boolean;
    v_result text;
    v_payout bigint := 0;
    v_game_status text := 'settled';
begin
    select bs.*
    into v_session
    from public.blackjack_sessions as bs
    where bs.game_id = p_game_id
    for update;

    if not found then
        return pg_catalog.jsonb_build_object('game_status', 'game_not_found');
    end if;
    if v_session.game_state = 'settled' then
        return public.blackjack_session_response(p_game_id, 'already_settled');
    end if;
    if v_session.game_state <> 'settling' then
        return public.blackjack_session_response(p_game_id, 'not_ready');
    end if;

    select ep.wallet_balance
    into v_wallet_balance
    from public.economy_profiles as ep
    where ep.discord_user_id = v_session.discord_user_id
    for update;

    if not found then
        return pg_catalog.jsonb_build_object('game_status', 'profile_not_found');
    end if;

    v_player_score := public.blackjack_hand_value(v_session.player_cards);
    v_dealer_score := public.blackjack_hand_value(v_session.dealer_cards);
    v_player_natural := pg_catalog.cardinality(v_session.player_cards) = 2
        and v_player_score = 21;
    v_dealer_natural := pg_catalog.cardinality(v_session.dealer_cards) = 2
        and v_dealer_score = 21;

    if v_session.finish_reason = 'player_bust' or v_player_score > 21 then
        v_result := 'loss';
    elsif v_player_natural and not v_dealer_natural then
        v_result := 'natural_blackjack';
        v_payout := pg_catalog.floor(v_session.bet_amount::numeric * 5 / 2)::bigint;
    elsif v_dealer_score > 21 or v_player_score > v_dealer_score then
        v_result := 'win';
        v_payout := v_session.bet_amount * 2;
    elsif v_player_score = v_dealer_score then
        v_result := 'dealer_wins_tie';
    else
        v_result := 'loss';
    end if;

    if v_payout > 0 then
        if v_wallet_balance::numeric + v_payout::numeric > 9223372036854775807::numeric then
            v_game_status := 'balance_limit_reached';
            v_result := 'balance_limit_reached';
            v_payout := 0;
        else
            v_wallet_balance := v_wallet_balance + v_payout;
            update public.economy_profiles as ep
            set wallet_balance = v_wallet_balance
            where ep.discord_user_id = v_session.discord_user_id;
        end if;
    end if;

    update public.blackjack_sessions as bs
    set game_state = 'settled',
        settlement_result = v_result,
        gross_payout = v_payout,
        wallet_balance_after = v_wallet_balance,
        settled_at = pg_catalog.clock_timestamp()
    where bs.game_id = p_game_id;

    return public.blackjack_session_response(p_game_id, v_game_status);
end;
$function$;

create or replace function public.start_blackjack_game(
    p_discord_user_id text,
    p_game_id uuid,
    p_request_id uuid,
    p_bet bigint
)
returns jsonb
language plpgsql
security invoker
set search_path = ''
as $function$
declare
    v_wallet_balance bigint;
    v_last_gambling_at timestamptz;
    v_now timestamptz;
    v_next_gambling_at timestamptz;
    v_existing_game_id uuid;
    v_existing_user_id text;
    v_deck text[] := array[]::text[];
    v_player_cards text[];
    v_dealer_cards text[];
    v_ranks text[] := array['A','2','3','4','5','6','7','8','9','10','J','Q','K'];
    v_suits text[] := array['♠','♥','♦','♣'];
    v_rank text;
    v_suit text;
    v_card text;
    v_swap text;
    v_i integer;
    v_j integer;
    v_player_score integer;
begin
    if p_discord_user_id is null or pg_catalog.btrim(p_discord_user_id) = ''
       or p_game_id is null or p_request_id is null then
        return pg_catalog.jsonb_build_object('game_status', 'invalid_request');
    end if;
    if p_bet is null or p_bet < 10 or p_bet > 6666666 then
        return pg_catalog.jsonb_build_object('game_status', 'invalid_bet');
    end if;

    select bs.game_id, bs.discord_user_id
    into v_existing_game_id, v_existing_user_id
    from public.blackjack_sessions as bs
    where bs.start_request_id = p_request_id;
    if found then
        if v_existing_user_id <> p_discord_user_id then
            return pg_catalog.jsonb_build_object('game_status', 'invalid_request');
        end if;
        return public.blackjack_session_response(
            v_existing_game_id,
            case when (select bs.game_state from public.blackjack_sessions as bs
                       where bs.game_id = v_existing_game_id) = 'settled'
                then 'already_settled' else 'started' end
        );
    end if;

    if exists (select 1 from public.blackjack_sessions as bs where bs.game_id = p_game_id) then
        return pg_catalog.jsonb_build_object('game_status', 'invalid_request');
    end if;

    select ep.wallet_balance, ep.last_gambling_at
    into v_wallet_balance, v_last_gambling_at
    from public.economy_profiles as ep
    where ep.discord_user_id = p_discord_user_id
    for update;
    if not found then
        return pg_catalog.jsonb_build_object('game_status', 'profile_not_found');
    end if;

    v_now := pg_catalog.clock_timestamp();
    if v_last_gambling_at is not null
       and v_last_gambling_at + interval '5 seconds' > v_now then
        return pg_catalog.jsonb_build_object(
            'game_status', 'cooldown',
            'wallet_balance_after', v_wallet_balance,
            'next_gambling_at', v_last_gambling_at + interval '5 seconds'
        );
    end if;
    if v_wallet_balance < p_bet then
        return pg_catalog.jsonb_build_object(
            'game_status', 'insufficient_funds',
            'wallet_balance_after', v_wallet_balance
        );
    end if;

    foreach v_suit in array v_suits loop
        foreach v_rank in array v_ranks loop
            v_deck := pg_catalog.array_append(v_deck, v_rank || v_suit);
        end loop;
    end loop;

    for v_i in reverse 52..2 loop
        v_j := public.blackjack_random_below(v_i) + 1;
        v_swap := v_deck[v_i];
        v_deck[v_i] := v_deck[v_j];
        v_deck[v_j] := v_swap;
    end loop;

    v_player_cards := array[v_deck[1], v_deck[3]];
    v_dealer_cards := array[v_deck[2], v_deck[4]];
    v_next_gambling_at := v_now + interval '5 seconds';
    v_wallet_balance := v_wallet_balance - p_bet;

    update public.economy_profiles as ep
    set wallet_balance = v_wallet_balance,
        last_gambling_at = v_now
    where ep.discord_user_id = p_discord_user_id;

    v_player_score := public.blackjack_hand_value(v_player_cards);
    insert into public.blackjack_sessions (
        game_id, start_request_id, discord_user_id, bet_amount,
        deck, next_card_index, player_cards, dealer_cards, game_state,
        finish_reason, wallet_balance_after, next_gambling_at
    ) values (
        p_game_id, p_request_id, p_discord_user_id, p_bet,
        v_deck, 5, v_player_cards, v_dealer_cards,
        case when v_player_score = 21 then 'settling' else 'active' end,
        case when v_player_score = 21 then 'natural_blackjack' else null end,
        v_wallet_balance, v_next_gambling_at
    );

    if v_player_score = 21 then
        return public.settle_blackjack_game(p_game_id);
    end if;
    return public.blackjack_session_response(p_game_id, 'started');
end;
$function$;

create or replace function public.process_blackjack_action(
    p_game_id uuid,
    p_discord_user_id text,
    p_action text,
    p_action_id text
)
returns jsonb
language plpgsql
security invoker
set search_path = ''
as $function$
declare
    v_session public.blackjack_sessions%rowtype;
    v_existing_game_id uuid;
    v_existing_user_id text;
    v_cached_response jsonb;
    v_response jsonb;
    v_player_score integer;
    v_dealer_score integer;
    v_card text;
    v_index integer;
begin
    if p_game_id is null or p_discord_user_id is null
       or pg_catalog.btrim(p_discord_user_id) = ''
       or p_action_id is null or pg_catalog.btrim(p_action_id) = ''
       or p_action is null
       or p_action not in ('hit', 'stand') then
        return pg_catalog.jsonb_build_object('game_status', 'invalid_action');
    end if;

    select bs.*
    into v_session
    from public.blackjack_sessions as bs
    where bs.game_id = p_game_id
    for update;
    if not found then
        return pg_catalog.jsonb_build_object('game_status', 'game_not_found');
    end if;
    if v_session.discord_user_id <> p_discord_user_id then
        return pg_catalog.jsonb_build_object('game_status', 'not_owner');
    end if;

    insert into public.blackjack_action_requests (action_id, game_id, discord_user_id)
    values (p_action_id, p_game_id, p_discord_user_id)
    on conflict (action_id) do nothing;
    if not found then
        select ar.game_id, ar.discord_user_id, ar.response
        into v_existing_game_id, v_existing_user_id, v_cached_response
        from public.blackjack_action_requests as ar
        where ar.action_id = p_action_id;
        if v_existing_game_id <> p_game_id or v_existing_user_id <> p_discord_user_id then
            return pg_catalog.jsonb_build_object('game_status', 'invalid_action');
        end if;
        if v_cached_response is not null then
            return v_cached_response;
        end if;
    end if;

    if v_session.game_state = 'settled' then
        v_response := public.blackjack_session_response(p_game_id, 'already_settled');
    elsif v_session.game_state = 'settling' then
        v_response := public.settle_blackjack_game(p_game_id);
    elsif p_action = 'hit' then
        if v_session.next_card_index > pg_catalog.cardinality(v_session.deck) then
            v_response := public.blackjack_session_response(p_game_id, 'invalid_action');
        else
            v_card := v_session.deck[v_session.next_card_index];
            v_session.player_cards := pg_catalog.array_append(v_session.player_cards, v_card);
            v_session.next_card_index := v_session.next_card_index + 1;
            v_player_score := public.blackjack_hand_value(v_session.player_cards);
            if v_player_score > 21 then
                update public.blackjack_sessions as bs
                set player_cards = v_session.player_cards,
                    next_card_index = v_session.next_card_index,
                    game_state = 'settling',
                    finish_reason = 'player_bust'
                where bs.game_id = p_game_id;
                v_response := public.settle_blackjack_game(p_game_id);
            else
                update public.blackjack_sessions as bs
                set player_cards = v_session.player_cards,
                    next_card_index = v_session.next_card_index
                where bs.game_id = p_game_id;
                v_response := public.blackjack_session_response(p_game_id, 'active');
            end if;
        end if;
    else
        v_dealer_score := public.blackjack_hand_value(v_session.dealer_cards);
        v_index := v_session.next_card_index;
        while v_dealer_score < 21 and v_index <= pg_catalog.cardinality(v_session.deck) loop
            v_card := v_session.deck[v_index];
            v_session.dealer_cards := pg_catalog.array_append(v_session.dealer_cards, v_card);
            v_index := v_index + 1;
            v_dealer_score := public.blackjack_hand_value(v_session.dealer_cards);
        end loop;
        update public.blackjack_sessions as bs
        set dealer_cards = v_session.dealer_cards,
            next_card_index = v_index,
            game_state = 'settling',
            finish_reason = 'stand'
        where bs.game_id = p_game_id;
        v_response := public.settle_blackjack_game(p_game_id);
    end if;

    update public.blackjack_action_requests as ar
    set response = v_response
    where ar.action_id = p_action_id;
    return v_response;
end;
$function$;

create or replace function public.list_active_blackjack_sessions()
returns jsonb
language sql
security invoker
set search_path = ''
as $function$
    select coalesce(
        pg_catalog.jsonb_agg(
            pg_catalog.jsonb_build_object(
                'game_id', bs.game_id,
                'discord_user_id', bs.discord_user_id,
                'game_state', bs.game_state
            )
        ),
        '[]'::jsonb
    )
    from public.blackjack_sessions as bs
    where bs.game_state in ('active', 'settling');
$function$;

revoke all on function public.blackjack_random_below(integer)
    from public, anon, authenticated;
revoke all on function public.blackjack_hand_value(text[])
    from public, anon, authenticated;
revoke all on function public.blackjack_session_response(uuid, text)
    from public, anon, authenticated;
revoke all on function public.settle_blackjack_game(uuid)
    from public, anon, authenticated;
revoke all on function public.start_blackjack_game(text, uuid, uuid, bigint)
    from public, anon, authenticated;
revoke all on function public.process_blackjack_action(uuid, text, text, text)
    from public, anon, authenticated;
revoke all on function public.list_active_blackjack_sessions()
    from public, anon, authenticated;

grant execute on function public.blackjack_random_below(integer) to service_role;
grant execute on function public.blackjack_hand_value(text[]) to service_role;
grant execute on function public.blackjack_session_response(uuid, text) to service_role;
grant execute on function public.settle_blackjack_game(uuid) to service_role;
grant execute on function public.start_blackjack_game(text, uuid, uuid, bigint) to service_role;
grant execute on function public.process_blackjack_action(uuid, text, text, text) to service_role;
grant execute on function public.list_active_blackjack_sessions() to service_role;

commit;
