-- Migration 358: smart client search (word order, accents, punctuation, phone formats).
--
-- bz_fold(text) is the ONE folding function: the expression indexes below and the router query
-- both call it, so the index and the typed query fold identically. It lowers case, folds Latin
-- accents and the sharp s / ligatures by an explicit translate() map (no unaccent extension in
-- prod, and no locale-dependent character class: the map also lowers Latin and Cyrillic
-- capitals itself, so it behaves the same in a C-locale database), turns a fixed punctuation set
-- into spaces and collapses them. Letters of every other script are kept as they are.
-- bz_phone_core(text) is the same projection as backend.phone_lock.phone_core and migration 308
-- (digits, ONE leading 62 or 0 stripped), without the six-digit floor: search accepts
-- fragments, dedup does not.
-- NO column is added to clients or companies (a derived column would ride every SELECT * /
-- RETURNING * / audit snapshot): the search keys are IMMUTABLE functions of the existing
-- columns, served by EXPRESSION GIN pg_trgm indexes. The query must use the identical
-- expressions (the planner matches expressions, not intent); the key functions below are the
-- single definition both sides call.
-- IF bz_fold, bz_phone_core OR ANY bz_*_key FUNCTION CHANGES, A STORED INDEX ENTRY IS STALE:
-- the migration that changes it must end with REINDEX INDEX for each idx_*_smart_* index below.
-- Cost on prod: clients ~12.5k rows, companies ~1.8k. A plain CREATE INDEX takes SHARE, which
-- blocks writes (not reads) for the build (measured ~1.5 s for the clients key index, ~3 s for
-- all five, dominated by bz_fold at ~100 us per row); CREATE INDEX CONCURRENTLY cannot run
-- inside the runner transaction. lock_timeout bounds the wait for the lock, not the build.
-- Function environment: pg_catalog is always searched first, so unqualified built-ins are not
-- shadowed; the two settings that DID change results are fixed in the bodies (no backslash in any
-- string literal, explicit "C" collation for lower()), and a test compares the output across them.
-- depends: 357
SET LOCAL lock_timeout = '5s';
SET LOCAL search_path = public, pg_catalog;

-- The punctuation fold both paths end with. The class is assembled with chr() so that no
-- backslash sits in a string literal: with standard_conforming_strings = off a literal backslash
-- is parsed differently, and an IMMUTABLE function must not depend on a session setting. The
-- character class is explicit (ASCII punctuation, ASCII whitespace via [:space:], no-break
-- space, curly quotes, dashes, ellipsis, middle dot, bullet): the input is already in the "C"
-- collation, so [:space:] is ASCII-only and does not follow the database locale.
CREATE OR REPLACE FUNCTION public.bz_fold_spaces(txt text) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $fn$
    SELECT btrim(regexp_replace(
        txt,
        '[[:space:]!"#$%&''()*+,./:;<=>?@^_`{|}~ ‘’“”–—…·•'
            || chr(92) || '[' || chr(92) || chr(92) || chr(92) || ']' || chr(92) || '-]+',
        ' ', 'g'))
$fn$;

CREATE OR REPLACE FUNCTION public.bz_fold_full(txt text) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $fn$
    SELECT public.bz_fold_spaces(
        translate(
            lower(replace(replace(replace(replace(replace(replace(replace(replace(
                COALESCE(txt, ''),
                'ß', 'ss'), 'ẞ', 'ss'), 'æ', 'ae'), 'Æ', 'ae'), 'œ', 'oe'), 'Œ', 'oe'), 'þ', 'th'), 'Þ', 'th') COLLATE "C"),
            'ÀÁÂÃÄÅÇÈÉÊËÌÍÎÏÐÑÒÓÔÕÖØÙÚÛÜÝàáâãäåçèéêëìíîïðñòóôõöøùúûüýÿĀāĂăĄąĆćĈĉĊċČčĎďĐđĒēĔĕĖėĘęĚěĜĝĞğĠġĢģĤĥĦħĨĩĪīĬĭĮįİıĴĵĶķĸĹĺĻļĽľŁłŃńŅņŇňŊŋŌōŎŏŐőŔŕŖŗŘřŚśŜŝŞşŠšŢţŤťŦŧŨũŪūŬŭŮůŰűŲųŴŵŶŷŸŹźŻżŽžƠơƯưǍǎǏǐǑǒǓǔǕǖǗǘǙǚǛǜǞǟǠǡǦǧǨǩǪǫǬǭǰǴǵǸǹǺǻȀȁȂȃȄȅȆȇȈȉȊȋȌȍȎȏȐȑȒȓȔȕȖȗȘșȚțȞȟȦȧȨȩȪȫȬȭȮȯȰȱȲȳḀḁḂḃḄḅḆḇḈḉḊḋḌḍḎḏḐḑḒḓḔḕḖḗḘḙḚḛḜḝḞḟḠḡḢḣḤḥḦḧḨḩḪḫḬḭḮḯḰḱḲḳḴḵḶḷḸḹḺḻḼḽḾḿṀṁṂṃṄṅṆṇṈṉṊṋṌṍṎṏṐṑṒṓṔṕṖṗṘṙṚṛṜṝṞṟṠṡṢṣṤṥṦṧṨṩṪṫṬṭṮṯṰṱṲṳṴṵṶṷṸṹṺṻṼṽṾṿẀẁẂẃẄẅẆẇẈẉẊẋẌẍẎẏẐẑẒẓẔẕẖẗẘẙẠạẢảẤấẦầẨẩẪẫẬậẮắẰằẲẳẴẵẶặẸẹẺẻẼẽẾếỀềỂểỄễỆệỈỉỊịỌọỎỏỐốỒồỔổỖỗỘộỚớỜờỞởỠỡỢợỤụỦủỨứỪừỬửỮữỰựỲỳỴỵỶỷỸỹёЁЀЂЃЄЅІЇЈЉЊЋЌЍЎЏАБВГДЕЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯҐҒҔҖҘҚҜҞҠҢҤҦҨҪҬҮҰҲҴҶҸҺҼҾ̴̵̶̷̸̡̢̧̨̛̖̗̘̙̜̝̞̟̠̣̤̥̦̩̪̫̬̭̮̯̰̱̲̳̹̺̻̼͇͈͉͍͎̀́̂̃̄̅̆̇̈̉̊̋̌̍̎̏̐̑̒̓̔̽̾̿̀́͂̓̈́͆͊͋͌̕̚ͅ͏͓͔͕͖͙͚͐͑͒͗͛ͣͤͥͦͧͨͩͪͫͬͭͮͯ͘͜͟͢͝͞͠͡',
            'aaaaaaceeeeiiiidnoooooouuuuyaaaaaaceeeeiiiidnoooooouuuuyyaaaaaaccccccccddddeeeeeeeeeegggggggghhhhiiiiiiiiiijjkkkllllllllnnnnnnnnoooooorrrrrrssssssssttttttuuuuuuuuuuuuwwyyyzzzzzzoouuaaiioouuuuuuuuuuaaaaggkkoooojggnnaaaaaaeeeeiiiioooorrrruuuusstthhaaeeooooooooyyaabbbbbbccddddddddddeeeeeeeeeeffgghhhhhhhhhhiiiikkkkkkllllllllmmmmmmnnnnnnnnoooooooopppprrrrrrrrssssssssssttttttttuuuuuuuuuuvvvvwwwwwwwwwwxxxxyyzzzzzzhtwyaaaaaaaaaaaaaaaaaaaaaaaaeeeeeeeeeeeeeeeeiiiioooooooooooooooooooooooouuuuuuuuuuuuuuyyyyyyyyееѐђѓєѕіїјљњћќѝўџабвгдежзийклмнопрстуфхцчшщъыьэюяґғҕҗҙқҝҟҡңҥҧҩҫҭүұҳҵҷҹһҽҿ')
    )
$fn$;

-- ASCII fast path: the accent map cannot touch an ASCII character, so for an ASCII-only value
-- (the vast majority of names and e-mails) only lower() and the punctuation fold apply. The test
-- is octet_length = length (the database encoding is UTF8, checked by the migration guard), which
-- needs no regular expression and no session setting. Measured: the full path costs ~100 us per
-- value, which the expression-index recheck pays per candidate row. A test asserts
-- bz_fold = bz_fold_full on ASCII input.
CREATE OR REPLACE FUNCTION public.bz_fold(txt text) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $fn$
    SELECT CASE
        WHEN octet_length(COALESCE(txt, '')) = length(COALESCE(txt, ''))
            THEN public.bz_fold_spaces(lower(COALESCE(txt, '') COLLATE "C"))
        ELSE public.bz_fold_full(txt)
    END
$fn$;

CREATE OR REPLACE FUNCTION public.bz_phone_core(txt text) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $fn$
    SELECT CASE
        WHEN regexp_replace(COALESCE(txt, ''), '[^0-9]', '', 'g') LIKE '62%'
            THEN substr(regexp_replace(COALESCE(txt, ''), '[^0-9]', '', 'g'), 3)
        WHEN regexp_replace(COALESCE(txt, ''), '[^0-9]', '', 'g') LIKE '0%'
            THEN substr(regexp_replace(COALESCE(txt, ''), '[^0-9]', '', 'g'), 2)
        ELSE regexp_replace(COALESCE(txt, ''), '[^0-9]', '', 'g')
    END
$fn$;

CREATE OR REPLACE FUNCTION public.bz_name_key(txt text) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $fn$
    SELECT replace(public.bz_fold(txt), ' ', '')
$fn$;

CREATE OR REPLACE FUNCTION public.bz_client_key(name text, email text) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $fn$
    SELECT replace(public.bz_fold(name), ' ', '') || '|' || replace(public.bz_fold(email), ' ', '')
$fn$;

CREATE OR REPLACE FUNCTION public.bz_phone_key(phone text, phone_normalized text, whatsapp text)
RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $fn$
    SELECT btrim(public.bz_phone_core(phone) || ' ' || public.bz_phone_core(phone_normalized)
                 || ' ' || public.bz_phone_core(whatsapp))
$fn$;

CREATE OR REPLACE FUNCTION public.bz_ident_key(nib text, npwp text) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $fn$
    SELECT btrim(regexp_replace(COALESCE(nib, ''), '[^0-9]', '', 'g') || ' '
                 || regexp_replace(COALESCE(npwp, ''), '[^0-9]', '', 'g'))
$fn$;

-- Ranking of a matched name, 0 best: 0 the name's words are exactly the typed words (any order),
-- 1 the name starts with the first typed word, 2 every typed word is inside the compact name,
-- 3 matched elsewhere (e-mail, phone, identifiers, linked company). plpgsql, not SQL, so bz_fold
-- runs ONCE per row (an inlined SQL function would repeat the translate() for every use).
CREATE OR REPLACE FUNCTION public.bz_name_rank(name text, tokens text[]) RETURNS integer
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE AS $fn$
DECLARE
    folded text := public.bz_fold(name);
    words text[] := string_to_array(folded, ' ');
    compact text := replace(folded, ' ', '');
    tok text;
BEGIN
    IF words @> tokens AND words <@ tokens THEN
        RETURN 0;
    END IF;
    IF left(folded, length(tokens[1])) = tokens[1] THEN
        RETURN 1;
    END IF;
    FOREACH tok IN ARRAY tokens LOOP
        IF position(tok IN compact) = 0 THEN
            RETURN 3;
        END IF;
    END LOOP;
    RETURN 2;
END
$fn$;

DO $migration$
DECLARE
    trgm_schema text;
BEGIN
    SELECT n.nspname INTO trgm_schema
    FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace
    WHERE e.extname = 'pg_trgm';
    IF trgm_schema IS NULL THEN
        RAISE NOTICE 'Migration 358: pg_trgm not installed; skipping the indexes (the search keeps the legacy path).';
        RETURN;
    END IF;
    IF pg_encoding_to_char((SELECT encoding FROM pg_database WHERE datname = current_database())) <> 'UTF8' THEN
        RAISE NOTICE 'Migration 358: database encoding is not UTF8 (the ASCII fast path assumes it); skipping the indexes.';
        RETURN;
    END IF;

    IF (
        SELECT count(*) FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'clients'
          AND column_name IN ('full_name', 'email', 'phone', 'phone_normalized',
                              'whatsapp', 'nib', 'npwp')
    ) = 7 THEN
        EXECUTE format('CREATE INDEX IF NOT EXISTS idx_clients_smart_key ON public.clients USING gin ((public.bz_client_key(full_name, email)) %I.gin_trgm_ops)', trgm_schema);
        EXECUTE format('CREATE INDEX IF NOT EXISTS idx_clients_smart_phone ON public.clients USING gin ((public.bz_phone_key(phone, phone_normalized, whatsapp)) %I.gin_trgm_ops)', trgm_schema);
        EXECUTE format('CREATE INDEX IF NOT EXISTS idx_clients_smart_ident ON public.clients USING gin ((public.bz_ident_key(nib, npwp)) %I.gin_trgm_ops)', trgm_schema);
    ELSE
        RAISE NOTICE 'Migration 358: clients (or one of its columns) not present; skipping its indexes (CI/fresh schema).';
    END IF;

    IF (
        SELECT count(*) FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'companies'
          AND column_name IN ('company_name', 'nib', 'npwp_company')
    ) = 3 THEN
        EXECUTE format('CREATE INDEX IF NOT EXISTS idx_companies_smart_name ON public.companies USING gin ((public.bz_name_key(company_name)) %I.gin_trgm_ops)', trgm_schema);
        EXECUTE format('CREATE INDEX IF NOT EXISTS idx_companies_smart_ident ON public.companies USING gin ((public.bz_ident_key(nib, npwp_company)) %I.gin_trgm_ops)', trgm_schema);
    ELSE
        RAISE NOTICE 'Migration 358: companies (or one of its columns) not present; skipping its indexes.';
    END IF;
END
$migration$;

-- === ROLLBACK ===
-- The indexes are derived data: dropping them loses nothing. The functions are dropped last.
DROP INDEX IF EXISTS public.idx_companies_smart_ident;
DROP INDEX IF EXISTS public.idx_companies_smart_name;
DROP INDEX IF EXISTS public.idx_clients_smart_ident;
DROP INDEX IF EXISTS public.idx_clients_smart_phone;
DROP INDEX IF EXISTS public.idx_clients_smart_key;
DROP FUNCTION IF EXISTS public.bz_name_rank(text, text[]);
DROP FUNCTION IF EXISTS public.bz_ident_key(text, text);
DROP FUNCTION IF EXISTS public.bz_phone_key(text, text, text);
DROP FUNCTION IF EXISTS public.bz_client_key(text, text);
DROP FUNCTION IF EXISTS public.bz_name_key(text);
DROP FUNCTION IF EXISTS public.bz_phone_core(text);
DROP FUNCTION IF EXISTS public.bz_fold(text);
DROP FUNCTION IF EXISTS public.bz_fold_full(text);
DROP FUNCTION IF EXISTS public.bz_fold_spaces(text);
