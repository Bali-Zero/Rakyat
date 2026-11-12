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
-- depends: 357
SET LOCAL lock_timeout = '5s';
SET LOCAL search_path = public, pg_catalog;

CREATE OR REPLACE FUNCTION public.bz_fold_full(txt text) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $fn$
    SELECT btrim(regexp_replace(
        translate(
            lower(replace(replace(replace(replace(replace(replace(replace(replace(
                COALESCE(txt, ''),
                'ß', 'ss'), 'ẞ', 'ss'), 'æ', 'ae'), 'Æ', 'ae'), 'œ', 'oe'), 'Œ', 'oe'), 'þ', 'th'), 'Þ', 'th')),
            'ÀÁÂÃÄÅÇÈÉÊËÌÍÎÏÐÑÒÓÔÕÖØÙÚÛÜÝàáâãäåçèéêëìíîïðñòóôõöøùúûüýÿĀāĂăĄąĆćĈĉĊċČčĎďĐđĒēĔĕĖėĘęĚěĜĝĞğĠġĢģĤĥĦħĨĩĪīĬĭĮįİıĴĵĶķĸĹĺĻļĽľŁłŃńŅņŇňŊŋŌōŎŏŐőŔŕŖŗŘřŚśŜŝŞşŠšŢţŤťŦŧŨũŪūŬŭŮůŰűŲųŴŵŶŷŸŹźŻżŽžƠơƯưǍǎǏǐǑǒǓǔǕǖǗǘǙǚǛǜǞǟǠǡǦǧǨǩǪǫǬǭǰǴǵǸǹǺǻȀȁȂȃȄȅȆȇȈȉȊȋȌȍȎȏȐȑȒȓȔȕȖȗȘșȚțȞȟȦȧȨȩȪȫȬȭȮȯȰȱȲȳḀḁḂḃḄḅḆḇḈḉḊḋḌḍḎḏḐḑḒḓḔḕḖḗḘḙḚḛḜḝḞḟḠḡḢḣḤḥḦḧḨḩḪḫḬḭḮḯḰḱḲḳḴḵḶḷḸḹḺḻḼḽḾḿṀṁṂṃṄṅṆṇṈṉṊṋṌṍṎṏṐṑṒṓṔṕṖṗṘṙṚṛṜṝṞṟṠṡṢṣṤṥṦṧṨṩṪṫṬṭṮṯṰṱṲṳṴṵṶṷṸṹṺṻṼṽṾṿẀẁẂẃẄẅẆẇẈẉẊẋẌẍẎẏẐẑẒẓẔẕẖẗẘẙẠạẢảẤấẦầẨẩẪẫẬậẮắẰằẲẳẴẵẶặẸẹẺẻẼẽẾếỀềỂểỄễỆệỈỉỊịỌọỎỏỐốỒồỔổỖỗỘộỚớỜờỞởỠỡỢợỤụỦủỨứỪừỬửỮữỰựỲỳỴỵỶỷỸỹёЁЀЂЃЄЅІЇЈЉЊЋЌЍЎЏАБВГДЕЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯҐҒҔҖҘҚҜҞҠҢҤҦҨҪҬҮҰҲҴҶҸҺҼҾ̴̵̶̷̸̡̢̧̨̛̖̗̘̙̜̝̞̟̠̣̤̥̦̩̪̫̬̭̮̯̰̱̲̳̹̺̻̼͇͈͉͍͎̀́̂̃̄̅̆̇̈̉̊̋̌̍̎̏̐̑̒̓̔̽̾̿̀́͂̓̈́͆͊͋͌̕̚ͅ͏͓͔͕͖͙͚͐͑͒͗͛ͣͤͥͦͧͨͩͪͫͬͭͮͯ͘͜͟͢͝͞͠͡',
            'aaaaaaceeeeiiiidnoooooouuuuyaaaaaaceeeeiiiidnoooooouuuuyyaaaaaaccccccccddddeeeeeeeeeegggggggghhhhiiiiiiiiiijjkkkllllllllnnnnnnnnoooooorrrrrrssssssssttttttuuuuuuuuuuuuwwyyyzzzzzzoouuaaiioouuuuuuuuuuaaaaggkkoooojggnnaaaaaaeeeeiiiioooorrrruuuusstthhaaeeooooooooyyaabbbbbbccddddddddddeeeeeeeeeeffgghhhhhhhhhhiiiikkkkkkllllllllmmmmmmnnnnnnnnoooooooopppprrrrrrrrssssssssssttttttttuuuuuuuuuuvvvvwwwwwwwwwwxxxxyyzzzzzzhtwyaaaaaaaaaaaaaaaaaaaaaaaaeeeeeeeeeeeeeeeeiiiioooooooooooooooooooooooouuuuuuuuuuuuuuyyyyyyyyееѐђѓєѕіїјљњћќѝўџабвгдежзийклмнопрстуфхцчшщъыьэюяґғҕҗҙқҝҟҡңҥҧҩҫҭүұҳҵҷҹһҽҿ'),
        '[[:space:]!"#$%&''()*+,./:;<=>?@\[\\\]^_`{|}~\-‘’“”–—…·•]+', ' ', 'g'))
$fn$;

-- ASCII fast path: the accent map cannot touch an ASCII character, so for an ASCII-only value
-- (the vast majority of names, e-mails) only lower() and the punctuation fold apply. Measured:
-- the full path costs ~100 us per value, which the expression-index recheck pays per candidate row.
-- A test asserts bz_fold = bz_fold_full on ASCII input.
CREATE OR REPLACE FUNCTION public.bz_fold(txt text) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $fn$
    SELECT CASE
        WHEN COALESCE(txt, '') ~ '^[\x01-\x7f]*$' THEN btrim(regexp_replace(
            lower(COALESCE(txt, '')),
            '[[:space:]!"#$%&''()*+,./:;<=>?@\[\\\]^_`{|}~\-‘’“”–—…·•]+', ' ', 'g'))
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
    words text[] := string_to_array(public.bz_fold(name), ' ');
    compact text := replace(public.bz_fold(name), ' ', '');
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
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm') THEN
        RAISE NOTICE 'Migration 358: pg_trgm not installed; skipping the indexes (the search keeps the legacy path).';
        RETURN;
    END IF;

    IF (
        SELECT count(*) FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'clients'
          AND column_name IN ('full_name', 'email', 'phone', 'phone_normalized',
                              'whatsapp', 'nib', 'npwp')
    ) = 7 THEN
        CREATE INDEX IF NOT EXISTS idx_clients_smart_key ON public.clients
            USING gin ((public.bz_client_key(full_name, email)) gin_trgm_ops);
        CREATE INDEX IF NOT EXISTS idx_clients_smart_phone ON public.clients
            USING gin ((public.bz_phone_key(phone, phone_normalized, whatsapp)) gin_trgm_ops);
        CREATE INDEX IF NOT EXISTS idx_clients_smart_ident ON public.clients
            USING gin ((public.bz_ident_key(nib, npwp)) gin_trgm_ops);
    ELSE
        RAISE NOTICE 'Migration 358: clients (or one of its columns) not present; skipping its indexes (CI/fresh schema).';
    END IF;

    IF (
        SELECT count(*) FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'companies'
          AND column_name IN ('company_name', 'nib', 'npwp_company')
    ) = 3 THEN
        CREATE INDEX IF NOT EXISTS idx_companies_smart_name ON public.companies
            USING gin ((public.bz_name_key(company_name)) gin_trgm_ops);
        CREATE INDEX IF NOT EXISTS idx_companies_smart_ident ON public.companies
            USING gin ((public.bz_ident_key(nib, npwp_company)) gin_trgm_ops);
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
