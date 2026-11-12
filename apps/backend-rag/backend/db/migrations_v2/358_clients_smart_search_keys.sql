-- Migration 358: smart client search keys (word order, accents, punctuation, phone formats).
--
-- bz_fold(text) is the ONE folding function: it folds the stored keys here and the typed
-- query in the router, so the two can never drift. It lowers case, folds Latin accents and
-- the sharp s / ligatures by an explicit translate() map (no unaccent extension in prod,
-- and no locale-dependent character class: the map also lowers Latin and Cyrillic capitals
-- itself, so it behaves the same in a C-locale database), turns a fixed punctuation set into
-- spaces and collapses them. Letters of every other script are kept as they are.
-- bz_phone_core(text) is the same projection as backend.phone_lock.phone_core and migration
-- 308 (digits, ONE leading 62 or 0 stripped), without the six-digit floor: search accepts
-- fragments, dedup does not.
-- GENERATED STORED key columns carry the folded text, so they cannot drift from the columns
-- they read and need no trigger: clients.name_words (folded name, words separated by one
-- space: used for ranking), name_key (compact folded name), search_key (name and
-- e-mail), phone_key, ident_key, and companies.name_key / ident_key. Folding on the fly was
-- measured at ~100 ms per query on 12.5k clients + 1.8k companies (translate over a ~700
-- character map costs ~50 us per value); stored keys make the search a plain LIKE.
-- `clients` is about 12.5k rows (~50 MB) and `companies` 1.8k, the rewrites take
-- milliseconds; lock_timeout keeps them from queueing behind a long transaction.
-- depends: 357
SET LOCAL lock_timeout = '5s';
SET LOCAL search_path = public, pg_catalog;

CREATE OR REPLACE FUNCTION public.bz_fold(txt text) RETURNS text
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

DO $migration$
BEGIN
    IF (
        SELECT count(*) FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'clients'
          AND column_name IN ('full_name', 'email', 'phone', 'phone_normalized',
                              'whatsapp', 'nib', 'npwp')
    ) < 7 THEN
        RAISE NOTICE 'Migration 358: clients (or one of its columns) not present; skipping the keys (CI/fresh schema).';
        RETURN;
    END IF;

    ALTER TABLE public.clients
        ADD COLUMN IF NOT EXISTS name_words text GENERATED ALWAYS AS (
            public.bz_fold(full_name)
        ) STORED,
        ADD COLUMN IF NOT EXISTS name_key text GENERATED ALWAYS AS (
            replace(public.bz_fold(full_name), ' ', '')
        ) STORED,
        ADD COLUMN IF NOT EXISTS search_key text GENERATED ALWAYS AS (
            replace(public.bz_fold(full_name), ' ', '') || '|' || replace(public.bz_fold(email), ' ', '')
        ) STORED,
        ADD COLUMN IF NOT EXISTS phone_key text GENERATED ALWAYS AS (
            btrim(public.bz_phone_core(phone) || ' ' || public.bz_phone_core(phone_normalized)
                  || ' ' || public.bz_phone_core(whatsapp))
        ) STORED,
        ADD COLUMN IF NOT EXISTS ident_key text GENERATED ALWAYS AS (
            btrim(regexp_replace(COALESCE(nib, ''), '[^0-9]', '', 'g') || ' '
                  || regexp_replace(COALESCE(npwp, ''), '[^0-9]', '', 'g'))
        ) STORED;

    IF (
        SELECT count(*) FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'companies'
          AND column_name IN ('company_name', 'nib', 'npwp_company')
    ) < 3 THEN
        RAISE NOTICE 'Migration 358: companies (or one of its columns) not present; skipping its keys.';
        RETURN;
    END IF;

    ALTER TABLE public.companies
        ADD COLUMN IF NOT EXISTS name_key text GENERATED ALWAYS AS (
            replace(public.bz_fold(company_name), ' ', '')
        ) STORED,
        ADD COLUMN IF NOT EXISTS ident_key text GENERATED ALWAYS AS (
            btrim(regexp_replace(COALESCE(nib, ''), '[^0-9]', '', 'g') || ' '
                  || regexp_replace(COALESCE(npwp_company, ''), '[^0-9]', '', 'g'))
        ) STORED;
END
$migration$;

-- === ROLLBACK ===
-- The columns are derived data: dropping them loses nothing. The functions are dropped last.
ALTER TABLE IF EXISTS public.companies DROP COLUMN IF EXISTS ident_key;
ALTER TABLE IF EXISTS public.companies DROP COLUMN IF EXISTS name_key;
ALTER TABLE IF EXISTS public.clients DROP COLUMN IF EXISTS ident_key;
ALTER TABLE IF EXISTS public.clients DROP COLUMN IF EXISTS phone_key;
ALTER TABLE IF EXISTS public.clients DROP COLUMN IF EXISTS search_key;
ALTER TABLE IF EXISTS public.clients DROP COLUMN IF EXISTS name_key;
ALTER TABLE IF EXISTS public.clients DROP COLUMN IF EXISTS name_words;
DROP FUNCTION IF EXISTS public.bz_phone_core(text);
DROP FUNCTION IF EXISTS public.bz_fold(text);
