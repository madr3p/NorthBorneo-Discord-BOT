begin;

create or replace function public.transfer_economy_funds(
    p_discord_user_id text,
    p_direction text,
    p_amount bigint
)
returns table (
    transfer_success boolean,
    transfer_status text,
    wallet_balance_after bigint,
    bank_balance_after bigint
)
language plpgsql
security invoker
set search_path = ''
as $function$
declare
    v_wallet_balance bigint;
    v_bank_balance bigint;
begin
    if p_amount is null or p_amount <= 0 then
        return query
        select false, 'invalid_amount'::text, null::bigint, null::bigint;
        return;
    end if;

    if p_direction is null or p_direction not in ('deposit', 'withdraw') then
        return query
        select false, 'invalid_direction'::text, null::bigint, null::bigint;
        return;
    end if;

    if p_direction = 'deposit' then
        update public.economy_profiles as ep
        set wallet_balance = ep.wallet_balance - p_amount,
            bank_balance = ep.bank_balance + p_amount
        where ep.discord_user_id = p_discord_user_id
          and ep.wallet_balance >= p_amount
          and ep.bank_balance <= 9223372036854775807::bigint - p_amount
        returning ep.wallet_balance, ep.bank_balance
        into v_wallet_balance, v_bank_balance;
    else
        update public.economy_profiles as ep
        set wallet_balance = ep.wallet_balance + p_amount,
            bank_balance = ep.bank_balance - p_amount
        where ep.discord_user_id = p_discord_user_id
          and ep.bank_balance >= p_amount
          and ep.wallet_balance <= 9223372036854775807::bigint - p_amount
        returning ep.wallet_balance, ep.bank_balance
        into v_wallet_balance, v_bank_balance;
    end if;

    if found then
        return query
        select true, 'success'::text, v_wallet_balance, v_bank_balance;
        return;
    end if;

    select ep.wallet_balance, ep.bank_balance
    into v_wallet_balance, v_bank_balance
    from public.economy_profiles as ep
    where ep.discord_user_id = p_discord_user_id;

    if found then
        if p_direction = 'deposit' then
            if v_wallet_balance < p_amount then
                return query
                select false, 'insufficient_funds'::text, v_wallet_balance, v_bank_balance;
            else
                return query
                select false, 'balance_limit_reached'::text, v_wallet_balance, v_bank_balance;
            end if;
        else
            if v_bank_balance < p_amount then
                return query
                select false, 'insufficient_funds'::text, v_wallet_balance, v_bank_balance;
            else
                return query
                select false, 'balance_limit_reached'::text, v_wallet_balance, v_bank_balance;
            end if;
        end if;
    else
        return query
        select false, 'profile_not_found'::text, null::bigint, null::bigint;
    end if;
end;
$function$;

revoke all on function public.transfer_economy_funds(text, text, bigint)
    from public, anon, authenticated;

grant execute on function public.transfer_economy_funds(text, text, bigint)
    to service_role;

commit;
