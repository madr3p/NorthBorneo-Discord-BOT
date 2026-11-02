begin;

alter table public.economy_profiles
    add column if not exists last_rob_at timestamptz;

create or replace function public.rob_player(
    p_robber_discord_user_id text,
    p_target_discord_user_id text
)
returns table (
    rob_success boolean,
    rob_status text,
    amount_stolen bigint,
    amount_fined bigint,
    robber_wallet_after bigint,
    robber_bank_after bigint,
    target_wallet_after bigint,
    target_bank_after bigint,
    next_rob_at timestamptz
)
language plpgsql
security invoker
set search_path = ''
as $function$
declare
    v_locked_profile record;
    v_robber_found boolean := false;
    v_target_found boolean := false;
    v_robber_wallet bigint;
    v_robber_bank bigint;
    v_robber_last_rob_at timestamptz;
    v_target_wallet bigint;
    v_target_bank bigint;
    v_next_rob_at timestamptz;
    v_now timestamptz;
    v_random_bytes bytea;
    v_random_value bigint;
    v_random_limit bigint;
    v_rob_success boolean;
    v_percentage bigint;
    v_target_total numeric;
    v_amount_stolen bigint := 0;
    v_amount_fined bigint := 0;
    v_fine bigint;
    v_target_wallet_deduction bigint := 0;
    v_target_bank_deduction bigint := 0;
    v_robber_wallet_deduction bigint := 0;
    v_robber_bank_deduction bigint := 0;
    v_robber_wallet_after bigint;
    v_robber_bank_after bigint;
    v_target_wallet_after bigint;
    v_target_bank_after bigint;
begin
    if p_robber_discord_user_id is null
       or p_target_discord_user_id is null
       or pg_catalog.btrim(p_robber_discord_user_id) = ''
       or pg_catalog.btrim(p_target_discord_user_id) = ''
       or p_robber_discord_user_id = p_target_discord_user_id then
        return query
        select null::boolean, 'invalid_target'::text, 0::bigint, 0::bigint,
               null::bigint, null::bigint, null::bigint, null::bigint,
               null::timestamptz;
        return;
    end if;

    for v_locked_profile in
        select ep.discord_user_id,
               ep.wallet_balance,
               ep.bank_balance,
               ep.last_rob_at
        from public.economy_profiles as ep
        where ep.discord_user_id in (
            p_robber_discord_user_id,
            p_target_discord_user_id
        )
        order by ep.discord_user_id
        for update
    loop
        if v_locked_profile.discord_user_id = p_robber_discord_user_id then
            v_robber_found := true;
            v_robber_wallet := v_locked_profile.wallet_balance;
            v_robber_bank := v_locked_profile.bank_balance;
            v_robber_last_rob_at := v_locked_profile.last_rob_at;
        elsif v_locked_profile.discord_user_id = p_target_discord_user_id then
            v_target_found := true;
            v_target_wallet := v_locked_profile.wallet_balance;
            v_target_bank := v_locked_profile.bank_balance;
        end if;
    end loop;

    if not v_robber_found then
        return query
        select null::boolean, 'robber_profile_not_found'::text, 0::bigint, 0::bigint,
               null::bigint, null::bigint,
               case when v_target_found then v_target_wallet else null::bigint end,
               case when v_target_found then v_target_bank else null::bigint end,
               null::timestamptz;
        return;
    end if;

    if not v_target_found then
        return query
        select null::boolean, 'target_profile_not_found'::text, 0::bigint, 0::bigint,
               v_robber_wallet, v_robber_bank, null::bigint, null::bigint,
               null::timestamptz;
        return;
    end if;

    v_now := pg_catalog.clock_timestamp();
    if v_robber_last_rob_at is not null
       and v_robber_last_rob_at + interval '5 minutes' > v_now then
        v_next_rob_at := v_robber_last_rob_at + interval '5 minutes';
        return query
        select null::boolean, 'cooldown'::text, 0::bigint, 0::bigint,
               v_robber_wallet, v_robber_bank, v_target_wallet, v_target_bank,
               v_next_rob_at;
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
    v_rob_success := (v_random_value % 100) < 50;

    if v_rob_success then
        v_target_total := v_target_wallet::numeric + v_target_bank::numeric;
        if v_target_total = 0 then
            v_next_rob_at := v_now + interval '5 minutes';
            update public.economy_profiles as ep
            set last_rob_at = v_now
            where ep.discord_user_id = p_robber_discord_user_id;

            return query
            select null::boolean, 'target_has_no_funds'::text, 0::bigint, 0::bigint,
                   v_robber_wallet, v_robber_bank, v_target_wallet, v_target_bank,
                   v_next_rob_at;
            return;
        end if;

        v_random_limit := (4294967296::bigint / 16) * 16;
        loop
            v_random_bytes := extensions.gen_random_bytes(4);
            v_random_value :=
                (pg_catalog.get_byte(v_random_bytes, 0)::bigint << 24)
                + (pg_catalog.get_byte(v_random_bytes, 1)::bigint << 16)
                + (pg_catalog.get_byte(v_random_bytes, 2)::bigint << 8)
                + pg_catalog.get_byte(v_random_bytes, 3)::bigint;
            exit when v_random_value < v_random_limit;
        end loop;
        v_percentage := 10 + (v_random_value % 16);
        v_amount_stolen := greatest(
            1::numeric,
            pg_catalog.floor(v_target_total * v_percentage::numeric / 100)
        )::bigint;

        if v_robber_wallet > 9223372036854775807::bigint - v_amount_stolen then
            v_next_rob_at := v_now + interval '5 minutes';
            update public.economy_profiles as ep
            set last_rob_at = v_now
            where ep.discord_user_id = p_robber_discord_user_id;

            return query
            select null::boolean, 'balance_limit_reached'::text, 0::bigint, 0::bigint,
                   v_robber_wallet, v_robber_bank, v_target_wallet, v_target_bank,
                   v_next_rob_at;
            return;
        end if;

        if v_amount_stolen > v_target_wallet then
            v_target_wallet_deduction := v_target_wallet;
        else
            v_target_wallet_deduction := v_amount_stolen;
        end if;
        v_target_bank_deduction := v_amount_stolen - v_target_wallet_deduction;

        v_robber_wallet_after := v_robber_wallet + v_amount_stolen;
        v_robber_bank_after := v_robber_bank;
        v_target_wallet_after := v_target_wallet - v_target_wallet_deduction;
        v_target_bank_after := v_target_bank - v_target_bank_deduction;
    else
        v_random_limit := (4294967296::bigint / 1001) * 1001;
        loop
            v_random_bytes := extensions.gen_random_bytes(4);
            v_random_value :=
                (pg_catalog.get_byte(v_random_bytes, 0)::bigint << 24)
                + (pg_catalog.get_byte(v_random_bytes, 1)::bigint << 16)
                + (pg_catalog.get_byte(v_random_bytes, 2)::bigint << 8)
                + pg_catalog.get_byte(v_random_bytes, 3)::bigint;
            exit when v_random_value < v_random_limit;
        end loop;
        v_fine := 1000 + (v_random_value % 1001);

        if v_fine > v_robber_wallet then
            v_robber_wallet_deduction := v_robber_wallet;
        else
            v_robber_wallet_deduction := v_fine;
        end if;
        v_robber_bank_deduction := v_fine - v_robber_wallet_deduction;
        if v_robber_bank_deduction > v_robber_bank then
            v_robber_bank_deduction := v_robber_bank;
        end if;

        v_amount_fined := v_robber_wallet_deduction + v_robber_bank_deduction;
        v_robber_wallet_after := v_robber_wallet - v_robber_wallet_deduction;
        v_robber_bank_after := v_robber_bank - v_robber_bank_deduction;
        v_target_wallet_after := v_target_wallet;
        v_target_bank_after := v_target_bank;
    end if;

    v_next_rob_at := v_now + interval '5 minutes';

    update public.economy_profiles as ep
    set wallet_balance = v_robber_wallet_after,
        bank_balance = v_robber_bank_after,
        last_rob_at = v_now
    where ep.discord_user_id = p_robber_discord_user_id;

    if v_rob_success then
        update public.economy_profiles as ep
        set wallet_balance = v_target_wallet_after,
            bank_balance = v_target_bank_after
        where ep.discord_user_id = p_target_discord_user_id;
    end if;

    return query
    select v_rob_success,
           case when v_rob_success then 'success' else 'failure' end::text,
           v_amount_stolen,
           v_amount_fined,
           v_robber_wallet_after,
           v_robber_bank_after,
           v_target_wallet_after,
           v_target_bank_after,
           v_next_rob_at;
end;
$function$;

revoke all on function public.rob_player(text, text)
    from public, anon, authenticated;

grant execute on function public.rob_player(text, text)
    to service_role;

commit;
