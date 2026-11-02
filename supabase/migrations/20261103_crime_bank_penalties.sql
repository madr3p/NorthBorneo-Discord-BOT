begin;

drop function if exists public.commit_crime_attempt(text);

create function public.commit_crime_attempt(p_discord_user_id text)
returns table (
    crime_success boolean,
    crime_status text,
    amount_won bigint,
    amount_lost bigint,
    wallet_balance_after bigint,
    bank_balance_after bigint,
    next_crime_at timestamptz
)
language plpgsql
security invoker
set search_path = ''
as $function$
declare
    v_wallet_balance bigint;
    v_bank_balance bigint;
    v_updated_wallet_balance bigint;
    v_updated_bank_balance bigint;
    v_last_crime_at timestamptz;
    v_next_crime_at timestamptz;
    v_now timestamptz;
    v_random_bytes bytea;
    v_random_value bigint;
    v_random_limit bigint;
    v_success boolean;
    v_amount_won bigint := 0;
    v_amount_lost bigint := 0;
    v_penalty bigint;
    v_wallet_deduction bigint := 0;
    v_bank_deduction bigint := 0;
begin
    select ep.wallet_balance, ep.bank_balance, ep.last_crime_at
    into v_wallet_balance, v_bank_balance, v_last_crime_at
    from public.economy_profiles as ep
    where ep.discord_user_id = p_discord_user_id
    for update;

    if not found then
        return query
        select null::boolean, 'profile_not_found'::text, 0::bigint, 0::bigint,
               null::bigint, null::bigint, null::timestamptz;
        return;
    end if;

    v_now := pg_catalog.clock_timestamp();

    if v_last_crime_at is not null
       and v_last_crime_at + interval '1 minute' > v_now then
        v_next_crime_at := v_last_crime_at + interval '1 minute';
        return query
        select null::boolean, 'cooldown'::text, 0::bigint, 0::bigint,
               v_wallet_balance, v_bank_balance, v_next_crime_at;
        return;
    end if;

    v_random_limit := (4294967296::bigint / 100) * 100;
    loop
        v_random_bytes := extensions.gen_random_bytes(4);
        v_random_value :=
            (pg_catalog.get_byte(v_random_bytes, 0)::bigint << 24)
            + (pg_catalog.get_byte(v_random_bytes, 1)::bigint << 16)
            + (pg_catalog.get_byte(v_random_bytes, 2)::bigint << 8)
            + pg_catalog.get_byte(v_random_bytes, 3)::bigint;
        exit when v_random_value < v_random_limit;
    end loop;

    v_success := (v_random_value % 100) < 60;

    if v_success then
        v_random_limit := (4294967296::bigint / 701) * 701;
        loop
            v_random_bytes := extensions.gen_random_bytes(4);
            v_random_value :=
                (pg_catalog.get_byte(v_random_bytes, 0)::bigint << 24)
                + (pg_catalog.get_byte(v_random_bytes, 1)::bigint << 16)
                + (pg_catalog.get_byte(v_random_bytes, 2)::bigint << 8)
                + pg_catalog.get_byte(v_random_bytes, 3)::bigint;
            exit when v_random_value < v_random_limit;
        end loop;

        v_amount_won := 300 + (v_random_value % 701);
        if v_wallet_balance > 9223372036854775807::bigint - v_amount_won then
            v_next_crime_at := v_now + interval '1 minute';
            update public.economy_profiles as ep
            set last_crime_at = v_now
            where ep.discord_user_id = p_discord_user_id;

            return query
            select true, 'balance_limit_reached'::text, 0::bigint, 0::bigint,
                   v_wallet_balance, v_bank_balance, v_next_crime_at;
            return;
        end if;

        v_updated_wallet_balance := v_wallet_balance + v_amount_won;
        v_updated_bank_balance := v_bank_balance;
    else
        v_random_limit := (4294967296::bigint / 301) * 301;
        loop
            v_random_bytes := extensions.gen_random_bytes(4);
            v_random_value :=
                (pg_catalog.get_byte(v_random_bytes, 0)::bigint << 24)
                + (pg_catalog.get_byte(v_random_bytes, 1)::bigint << 16)
                + (pg_catalog.get_byte(v_random_bytes, 2)::bigint << 8)
                + pg_catalog.get_byte(v_random_bytes, 3)::bigint;
            exit when v_random_value < v_random_limit;
        end loop;

        v_penalty := 100 + (v_random_value % 301);
        if v_penalty > v_wallet_balance then
            v_wallet_deduction := v_wallet_balance;
        else
            v_wallet_deduction := v_penalty;
        end if;

        v_bank_deduction := v_penalty - v_wallet_deduction;
        if v_bank_deduction > v_bank_balance then
            v_bank_deduction := v_bank_balance;
        end if;

        v_amount_lost := v_wallet_deduction + v_bank_deduction;
        v_updated_wallet_balance := v_wallet_balance - v_wallet_deduction;
        v_updated_bank_balance := v_bank_balance - v_bank_deduction;
    end if;

    v_next_crime_at := v_now + interval '1 minute';

    update public.economy_profiles as ep
    set wallet_balance = v_updated_wallet_balance,
        bank_balance = v_updated_bank_balance,
        last_crime_at = v_now
    where ep.discord_user_id = p_discord_user_id;

    return query
    select v_success,
           case when v_success then 'success' else 'failure' end::text,
           v_amount_won,
           v_amount_lost,
           v_updated_wallet_balance,
           v_updated_bank_balance,
           v_next_crime_at;
end;
$function$;

revoke all on function public.commit_crime_attempt(text)
    from public, anon, authenticated;

grant execute on function public.commit_crime_attempt(text)
    to service_role;

commit;
