begin;

-- Remove only the Plinko RPC. Unexpected dependencies should stop this
-- cleanup so shared gambling objects remain protected.
drop function if exists public.play_plinko_game(text, uuid, bigint, text);

commit;
