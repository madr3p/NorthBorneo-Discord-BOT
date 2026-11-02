begin;

create schema if not exists extensions;
create extension if not exists pgcrypto with schema extensions;

alter table public.economy_profiles
    add column if not exists last_work_at timestamptz;

create or replace function public.claim_work_reward(p_discord_user_id text)
returns table (
    work_success boolean,
    work_status text,
    reward_amount bigint,
    wallet_balance_after bigint,
    next_work_at timestamptz
)
language plpgsql
security invoker
set search_path = ''
as $function$
declare
    v_wallet_balance bigint;
    v_last_work_at timestamptz;
    v_next_work_at timestamptz;
    v_now timestamptz;
    v_random_bytes bytea;
    v_random_value bigint;
    v_random_limit bigint := (4294967296::bigint / 401) * 401;
    v_reward bigint;
begin
    select ep.wallet_balance, ep.last_work_at
    into v_wallet_balance, v_last_work_at
    from public.economy_profiles as ep
    where ep.discord_user_id = p_discord_user_id
    for update;

    if not found then
        return query
        select false, 'profile_not_found'::text, 0::bigint, null::bigint, null::timestamptz;
        return;
    end if;

    v_now := pg_catalog.clock_timestamp();

    if v_last_work_at is not null
       and v_last_work_at + interval '1 minute' > v_now then
        v_next_work_at := v_last_work_at + interval '1 minute';
        return query
        select false, 'cooldown'::text, 0::bigint, v_wallet_balance, v_next_work_at;
        return;
    end if;

    loop
        v_random_bytes := extensions.gen_random_bytes(4);
        v_random_value :=
            (pg_catalog.get_byte(v_random_bytes, 0)::bigint << 24)
            + (pg_catalog.get_byte(v_random_bytes, 1)::bigint << 16)
            + (pg_catalog.get_byte(v_random_bytes, 2)::bigint << 8)
            + pg_catalog.get_byte(v_random_bytes, 3)::bigint;
        exit when v_random_value < v_random_limit;
    end loop;

    v_reward := 100 + (v_random_value % 401);

    if v_wallet_balance > 9223372036854775807::bigint - v_reward then
        return query
        select false, 'balance_limit_reached'::text, 0::bigint, v_wallet_balance, v_now;
        return;
    end if;

    v_wallet_balance := v_wallet_balance + v_reward;
    v_next_work_at := v_now + interval '1 minute';

    update public.economy_profiles as ep
    set wallet_balance = v_wallet_balance,
        last_work_at = v_now
    where ep.discord_user_id = p_discord_user_id;

    return query
    select true, 'success'::text, v_reward, v_wallet_balance, v_next_work_at;
end;
$function$;

revoke all on function public.claim_work_reward(text)
    from public, anon, authenticated;

grant execute on function public.claim_work_reward(text)
    to service_role;

commit;
