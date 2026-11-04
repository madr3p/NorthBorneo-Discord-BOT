begin;

create or replace function public.play_roulette_game(
    p_discord_user_id text,
    p_request_id uuid,
    p_bet bigint,
    p_choice text
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
    v_random_limit bigint := (4294967296::bigint / 37) * 37;
    v_number integer;
    v_choice text;
    v_color text;
    v_won boolean := false;
    v_payout bigint := 0;
    v_status text;
    v_result text;
    v_outcome text;
    v_request_inserted boolean;
    v_response jsonb;
begin
    if p_discord_user_id is null
       or pg_catalog.btrim(p_discord_user_id) = ''
       or p_request_id is null then
        return query
        select false, 'invalid_request'::text, null::text, null::text,
               p_bet, 0::bigint, null::bigint, null::timestamptz;
        return;
    end if;

    v_choice := pg_catalog.lower(pg_catalog.btrim(coalesce(p_choice, '')));
    if v_choice not in ('red', 'black', 'odd', 'even')
       and v_choice !~ '^(0|[1-9]|[12][0-9]|3[0-6])$' then
        return query
        select false, 'invalid_choice'::text, null::text, null::text,
               p_bet, 0::bigint, null::bigint, null::timestamptz;
        return;
    end if;
    if p_bet is null or p_bet < 10 or p_bet > 6666666 then
        return query
        select false, 'invalid_bet'::text, null::text, null::text,
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
            raise exception 'Roulette request result is unavailable';
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
    else
        v_now := pg_catalog.clock_timestamp();
        if v_last_gambling_at is not null
           and v_last_gambling_at + interval '5 seconds' > v_now then
            v_status := 'cooldown';
            v_next_gambling_at := v_last_gambling_at + interval '5 seconds';
        elsif v_wallet_balance < p_bet then
            v_status := 'insufficient_funds';
        else
            -- Rejection sampling makes the 0-36 wheel result unbiased.
            loop
                v_random_bytes := extensions.gen_random_bytes(4);
                v_random_value :=
                    (pg_catalog.get_byte(v_random_bytes, 0)::bigint << 24)
                    + (pg_catalog.get_byte(v_random_bytes, 1)::bigint << 16)
                    + (pg_catalog.get_byte(v_random_bytes, 2)::bigint << 8)
                    + pg_catalog.get_byte(v_random_bytes, 3)::bigint;
                exit when v_random_value < v_random_limit;
            end loop;

            v_number := (v_random_value % 37)::integer;
            if v_number = 0 then
                v_color := 'green';
            elsif v_number in (1, 3, 5, 7, 9, 12, 14, 16, 18,
                               19, 21, 23, 25, 27, 30, 32, 34, 36) then
                v_color := 'red';
            else
                v_color := 'black';
            end if;
            v_outcome := v_number::text || '|' || v_color;

            if v_choice in ('red', 'black') then
                v_won := v_color = v_choice;
            elsif v_choice = 'odd' then
                v_won := v_number <> 0 and v_number % 2 = 1;
            elsif v_choice = 'even' then
                v_won := v_number <> 0 and v_number % 2 = 0;
            else
                v_won := v_number::text = v_choice;
            end if;

            if v_won then
                v_result := 'win';
                if v_choice ~ '^(0|[1-9]|[12][0-9]|3[0-6])$' then
                    v_payout := p_bet * 36;
                else
                    v_payout := p_bet * 2;
                end if;
            else
                v_result := 'loss';
            end if;

            v_next_gambling_at := v_now + interval '5 seconds';
            if v_wallet_balance::numeric - p_bet::numeric + v_payout::numeric
               > 9223372036854775807::numeric then
                v_status := 'balance_limit_reached';
                v_payout := 0;
                v_wallet_after := v_wallet_balance;
                update public.economy_profiles as ep
                set last_gambling_at = v_now
                where ep.discord_user_id = p_discord_user_id;
            else
                v_status := 'settled';
                v_wallet_after := v_wallet_balance - p_bet + v_payout;
                update public.economy_profiles as ep
                set wallet_balance = v_wallet_after,
                    last_gambling_at = v_now
                where ep.discord_user_id = p_discord_user_id;
                v_wallet_balance := v_wallet_after;
            end if;
        end if;
    end if;

    v_response := pg_catalog.jsonb_build_object(
        'play_success', v_status = 'settled',
        'play_status', v_status,
        'game_result', v_result,
        'game_outcome', v_outcome,
        'bet_amount', p_bet,
        'gross_payout', v_payout,
        'wallet_balance_after', v_wallet_balance,
        'next_gambling_at', v_next_gambling_at
    );
    update public.gambling_idempotency as gi
    set response = v_response
    where gi.request_id = p_request_id;

    return query
    select v_status = 'settled', v_status, v_result, v_outcome,
           p_bet, v_payout, v_wallet_balance, v_next_gambling_at;
end;
$function$;

revoke all on function public.play_roulette_game(text, uuid, bigint, text)
    from public, anon, authenticated;
grant execute on function public.play_roulette_game(text, uuid, bigint, text)
    to service_role;

commit;
