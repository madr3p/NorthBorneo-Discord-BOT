begin;

create schema if not exists extensions;
create extension if not exists pgcrypto with schema extensions;

alter table public.economy_profiles
    add column if not exists last_gambling_at timestamptz;

-- Stores an opaque request key and its result so the same RPC request cannot
-- debit or credit a wallet twice if the client retries after a timeout.
create table if not exists public.gambling_idempotency (
    request_id uuid primary key,
    discord_user_id text not null,
    response jsonb,
    created_at timestamptz not null default pg_catalog.clock_timestamp()
);

alter table public.gambling_idempotency enable row level security;
revoke all on table public.gambling_idempotency
    from public, anon, authenticated;
grant select, insert, update on table public.gambling_idempotency
    to service_role;

create or replace function public.play_gambling_game(
    p_discord_user_id text,
    p_request_id uuid,
    p_game_type text,
    p_bet bigint,
    p_choice text default null
)
returns table (
    play_success boolean,
    play_status text,
    game_result text,
    game_outcome text,
    bet_amount bigint,
    gross_payout bigint,
    wallet_balance_after bigint,
    next_gambling_at timestamptz
)
language plpgsql
security invoker
set search_path = ''
as $function$
declare
    v_existing_user_id text;
    v_cached_response jsonb;
    v_wallet_balance bigint;
    v_wallet_after bigint;
    v_last_gambling_at timestamptz;
    v_now timestamptz;
    v_next_gambling_at timestamptz;
    v_random_bytes bytea;
    v_random_value bigint;
    v_random_limit bigint;
    v_roll bigint;
    v_payout bigint := 0;
    v_status text;
    v_result text;
    v_outcome text;
    v_choice text;
    v_symbols text[] := array['7️⃣', '💎', '🔔', '🍇', '🍋', '🍒'];
    v_reels text[] := array[]::text[];
    v_symbol_index integer;
    v_index integer;
    v_request_inserted boolean;
begin
    if p_discord_user_id is null
       or pg_catalog.btrim(p_discord_user_id) = ''
       or p_request_id is null then
        return query
        select false, 'invalid_request'::text, null::text, null::text,
               p_bet, 0::bigint, null::bigint, null::timestamptz;
        return;
    end if;

    if p_game_type is null or p_game_type not in ('coinflip', 'dice', 'slots') then
        return query
        select false, 'invalid_game'::text, null::text, null::text,
               p_bet, 0::bigint, null::bigint, null::timestamptz;
        return;
    end if;

    if p_bet is null or p_bet < 10 or p_bet > 6666666 then
        return query
        select false, 'invalid_bet'::text, null::text, null::text,
               p_bet, 0::bigint, null::bigint, null::timestamptz;
        return;
    end if;

    v_choice := pg_catalog.lower(pg_catalog.btrim(coalesce(p_choice, '')));
    if (p_game_type = 'coinflip' and v_choice not in ('heads', 'tails'))
       or (p_game_type = 'dice' and v_choice !~ '^[1-6]$')
       or (p_game_type = 'slots' and v_choice <> '') then
        return query
        select false, 'invalid_choice'::text, null::text, null::text,
               p_bet, 0::bigint, null::bigint, null::timestamptz;
        return;
    end if;

    insert into public.gambling_idempotency (request_id, discord_user_id)
    values (p_request_id, p_discord_user_id)
    on conflict (request_id) do nothing;
    v_request_inserted := found;

    if not v_request_inserted then
        select gi.discord_user_id, gi.response
        into v_existing_user_id, v_cached_response
        from public.gambling_idempotency as gi
        where gi.request_id = p_request_id;

        if v_existing_user_id is distinct from p_discord_user_id then
            return query
            select false, 'invalid_request'::text, null::text, null::text,
                   p_bet, 0::bigint, null::bigint, null::timestamptz;
            return;
        end if;

        if v_cached_response is null then
            raise exception 'Gambling request result is unavailable';
        end if;

        return query
        select (v_cached_response ->> 'play_success')::boolean,
               v_cached_response ->> 'play_status',
               v_cached_response ->> 'game_result',
               v_cached_response ->> 'game_outcome',
               (v_cached_response ->> 'bet_amount')::bigint,
               (v_cached_response ->> 'gross_payout')::bigint,
               (v_cached_response ->> 'wallet_balance_after')::bigint,
               (v_cached_response ->> 'next_gambling_at')::timestamptz;
        return;
    end if;

    select ep.wallet_balance, ep.last_gambling_at
    into v_wallet_balance, v_last_gambling_at
    from public.economy_profiles as ep
    where ep.discord_user_id = p_discord_user_id
    for update;

    if not found then
        v_status := 'profile_not_found';
        v_wallet_balance := null;
    else
        v_now := pg_catalog.clock_timestamp();
        if v_last_gambling_at is not null
           and v_last_gambling_at + interval '5 seconds' > v_now then
            v_status := 'cooldown';
            v_next_gambling_at := v_last_gambling_at + interval '5 seconds';
        elsif v_wallet_balance < p_bet then
            v_status := 'insufficient_funds';
        else
            -- Four cryptographic random bytes, reduced by rejection sampling
            -- to avoid modulo bias for each game's outcome range.
            if p_game_type = 'coinflip' then
                v_random_limit := (4294967296::bigint / 2) * 2;
                loop
                    v_random_bytes := extensions.gen_random_bytes(4);
                    v_random_value :=
                        (pg_catalog.get_byte(v_random_bytes, 0)::bigint << 24)
                        + (pg_catalog.get_byte(v_random_bytes, 1)::bigint << 16)
                        + (pg_catalog.get_byte(v_random_bytes, 2)::bigint << 8)
                        + pg_catalog.get_byte(v_random_bytes, 3)::bigint;
                    exit when v_random_value < v_random_limit;
                end loop;
                v_outcome := case when v_random_value % 2 = 0 then 'heads' else 'tails' end;
                if v_outcome = v_choice then
                    v_payout := pg_catalog.floor(p_bet::numeric * 19 / 10)::bigint;
                    v_result := 'win';
                else
                    v_result := 'loss';
                end if;
            elsif p_game_type = 'dice' then
                v_random_limit := (4294967296::bigint / 6) * 6;
                loop
                    v_random_bytes := extensions.gen_random_bytes(4);
                    v_random_value :=
                        (pg_catalog.get_byte(v_random_bytes, 0)::bigint << 24)
                        + (pg_catalog.get_byte(v_random_bytes, 1)::bigint << 16)
                        + (pg_catalog.get_byte(v_random_bytes, 2)::bigint << 8)
                        + pg_catalog.get_byte(v_random_bytes, 3)::bigint;
                    exit when v_random_value < v_random_limit;
                end loop;
                v_roll := 1 + (v_random_value % 6);
                v_outcome := v_roll::text;
                if v_outcome = v_choice then
                    v_payout := pg_catalog.floor(p_bet::numeric * 57 / 10)::bigint;
                    v_result := 'win';
                else
                    v_result := 'loss';
                end if;
            else
                for v_index in 1..3 loop
                    v_random_limit := (4294967296::bigint / 6) * 6;
                    loop
                        v_random_bytes := extensions.gen_random_bytes(4);
                        v_random_value :=
                            (pg_catalog.get_byte(v_random_bytes, 0)::bigint << 24)
                            + (pg_catalog.get_byte(v_random_bytes, 1)::bigint << 16)
                            + (pg_catalog.get_byte(v_random_bytes, 2)::bigint << 8)
                            + pg_catalog.get_byte(v_random_bytes, 3)::bigint;
                        exit when v_random_value < v_random_limit;
                    end loop;
                    v_symbol_index := 1 + (v_random_value % 6)::integer;
                    v_reels := pg_catalog.array_append(v_reels, v_symbols[v_symbol_index]);
                end loop;

                v_outcome := pg_catalog.array_to_string(v_reels, ' ');
                if v_reels[1] = v_reels[2] and v_reels[2] = v_reels[3] then
                    v_payout := case v_reels[1]
                        when '7️⃣' then p_bet * 10
                        when '💎' then p_bet * 7
                        when '🔔' then p_bet * 5
                        when '🍇' then p_bet * 3
                        else p_bet * 2
                    end;
                    v_result := 'win';
                elsif v_reels[1] = v_reels[2]
                   or v_reels[1] = v_reels[3]
                   or v_reels[2] = v_reels[3] then
                    v_payout := pg_catalog.floor(p_bet::numeric / 2)::bigint;
                    v_result := 'partial_payout';
                else
                    v_result := 'loss';
                end if;
            end if;

            v_wallet_after := v_wallet_balance - p_bet;
            v_next_gambling_at := v_now + interval '5 seconds';
            if v_wallet_after::numeric + v_payout::numeric > 9223372036854775807::numeric then
                v_status := 'balance_limit_reached';
                v_payout := 0;
                v_wallet_after := v_wallet_balance;
                update public.economy_profiles as ep
                set last_gambling_at = v_now
                where ep.discord_user_id = p_discord_user_id;
            else
                v_wallet_after := v_wallet_after + v_payout;
                v_status := 'settled';
                update public.economy_profiles as ep
                set wallet_balance = v_wallet_after,
                    last_gambling_at = v_now
                where ep.discord_user_id = p_discord_user_id;
                v_wallet_balance := v_wallet_after;
            end if;
        end if;
    end if;

    update public.gambling_idempotency as gi
    set response = pg_catalog.jsonb_build_object(
        'play_success', v_status = 'settled',
        'play_status', v_status,
        'game_result', v_result,
        'game_outcome', v_outcome,
        'bet_amount', p_bet,
        'gross_payout', v_payout,
        'wallet_balance_after', v_wallet_balance,
        'next_gambling_at', v_next_gambling_at
    )
    where gi.request_id = p_request_id;

    return query
    select v_status = 'settled', v_status, v_result, v_outcome,
           p_bet, v_payout, v_wallet_balance, v_next_gambling_at;
end;
$function$;

revoke all on function public.play_gambling_game(text, uuid, text, bigint, text)
    from public, anon, authenticated;
grant execute on function public.play_gambling_game(text, uuid, text, bigint, text)
    to service_role;

commit;
