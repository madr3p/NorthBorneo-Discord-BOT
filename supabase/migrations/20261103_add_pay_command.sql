begin;

create or replace function public.pay_player(
    p_sender_discord_user_id text,
    p_recipient_discord_user_id text,
    p_amount bigint
)
returns table (
    payment_success boolean,
    payment_status text,
    sender_wallet_after bigint,
    sender_bank_after bigint,
    recipient_bank_after bigint
)
language plpgsql
security invoker
set search_path = ''
as $function$
declare
    v_locked_profile record;
    v_sender_found boolean := false;
    v_recipient_found boolean := false;
    v_sender_wallet bigint;
    v_sender_bank bigint;
    v_recipient_bank bigint;
    v_sender_wallet_after bigint;
    v_sender_bank_after bigint;
    v_recipient_bank_after bigint;
    v_sender_total numeric;
    v_wallet_deduction bigint;
    v_bank_deduction bigint;
begin
    if p_sender_discord_user_id is null
       or p_recipient_discord_user_id is null
       or pg_catalog.btrim(p_sender_discord_user_id) = ''
       or pg_catalog.btrim(p_recipient_discord_user_id) = '' then
        return query
        select false, 'invalid_recipient'::text, null::bigint, null::bigint, null::bigint;
        return;
    end if;

    if p_sender_discord_user_id = p_recipient_discord_user_id then
        return query
        select false, 'self_payment'::text, null::bigint, null::bigint, null::bigint;
        return;
    end if;

    if p_amount is null or p_amount <= 0 then
        return query
        select false, 'invalid_amount'::text, null::bigint, null::bigint, null::bigint;
        return;
    end if;

    for v_locked_profile in
        select ep.discord_user_id, ep.wallet_balance, ep.bank_balance
        from public.economy_profiles as ep
        where ep.discord_user_id in (
            p_sender_discord_user_id,
            p_recipient_discord_user_id
        )
        order by ep.discord_user_id
        for update
    loop
        if v_locked_profile.discord_user_id = p_sender_discord_user_id then
            v_sender_found := true;
            v_sender_wallet := v_locked_profile.wallet_balance;
            v_sender_bank := v_locked_profile.bank_balance;
        elsif v_locked_profile.discord_user_id = p_recipient_discord_user_id then
            v_recipient_found := true;
            v_recipient_bank := v_locked_profile.bank_balance;
        end if;
    end loop;

    if not v_sender_found then
        return query
        select false, 'sender_profile_not_found'::text,
               null::bigint, null::bigint,
               case when v_recipient_found then v_recipient_bank else null::bigint end;
        return;
    end if;

    if not v_recipient_found then
        return query
        select false, 'recipient_profile_not_found'::text,
               v_sender_wallet, v_sender_bank, null::bigint;
        return;
    end if;

    v_sender_total := v_sender_wallet::numeric + v_sender_bank::numeric;
    if v_sender_total < p_amount::numeric then
        return query
        select false, 'insufficient_funds'::text,
               v_sender_wallet, v_sender_bank, v_recipient_bank;
        return;
    end if;

    if v_recipient_bank > 9223372036854775807::bigint - p_amount then
        return query
        select false, 'balance_limit_reached'::text,
               v_sender_wallet, v_sender_bank, v_recipient_bank;
        return;
    end if;

    if p_amount > v_sender_wallet then
        v_wallet_deduction := v_sender_wallet;
    else
        v_wallet_deduction := p_amount;
    end if;
    v_bank_deduction := p_amount - v_wallet_deduction;

    v_sender_wallet_after := v_sender_wallet - v_wallet_deduction;
    v_sender_bank_after := v_sender_bank - v_bank_deduction;
    v_recipient_bank_after := v_recipient_bank + p_amount;

    update public.economy_profiles as ep
    set wallet_balance = v_sender_wallet_after,
        bank_balance = v_sender_bank_after
    where ep.discord_user_id = p_sender_discord_user_id;

    update public.economy_profiles as ep
    set bank_balance = v_recipient_bank_after
    where ep.discord_user_id = p_recipient_discord_user_id;

    return query
    select true, 'success'::text,
           v_sender_wallet_after, v_sender_bank_after, v_recipient_bank_after;
end;
$function$;

revoke all on function public.pay_player(text, text, bigint)
    from public, anon, authenticated;

grant execute on function public.pay_player(text, text, bigint)
    to service_role;

commit;
