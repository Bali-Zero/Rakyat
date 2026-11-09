/**
 * Google Consent Mode v2 bootstrap for the root layout (COM-253, reverts DAI-104).
 *
 * Every category defaults to "denied". The marketing hosts keep their old
 * behaviour: an automatic `analytics_storage: granted` update right after the
 * default. The client portal never gets that automatic grant — not on
 * my.balizero.com (or any subdomain of it) and not on any path that starts
 * with /portal (DAI-115: /portal, /portal/*, and /portal-anything alike).
 *
 * Fail-closed: the grant is sent only when the host is positively known to be a
 * non-portal Bali Zero host. An unknown, empty or unreadable host (preview URLs,
 * localhost, a proxy that rewrote the Host) is treated like the portal.
 *
 * Runs as a raw inline <script> in <head>, before gtag.js loads, so it is plain
 * ES5 and defines the global `gtag` the way Google's snippet does.
 */

/** Hosts that must never receive the automatic analytics grant. */
export const PORTAL_HOSTS = ["my.balizero.com", "www.my.balizero.com"] as const;

/** Exact hosts (besides *.balizero.com) that keep the automatic grant. */
export const GRANT_EXACT_HOSTS = [
  "balizero.com",
  "nuzantara.co.id",
  "www.nuzantara.co.id",
] as const;

export const consentModeScript =
  "window.dataLayer=window.dataLayer||[];" +
  "function gtag(){window.dataLayer.push(arguments);}" +
  "gtag('consent','default',{'analytics_storage':'denied','ad_storage':'denied','ad_user_data':'denied','ad_personalization':'denied','wait_for_update':500});" +
  `(function(){try{var P=${JSON.stringify(PORTAL_HOSTS)};var G=${JSON.stringify(GRANT_EXACT_HOSTS)};` +
  "var l=window.location||{};" +
  "var h=String(l.hostname||'').toLowerCase().replace(/\\.$/,'');" +
  "var p=String(l.pathname||'').toLowerCase();" +
  "if(!h||P.indexOf(h)!==-1||/\\.my\\.balizero\\.com$/.test(h))return;" +
  "if(p.indexOf('/portal')===0)return;" +
  "var known=G.indexOf(h)!==-1||/^[a-z0-9-]+(\\.[a-z0-9-]+)*\\.balizero\\.com$/.test(h);" +
  "if(!known)return;" +
  "gtag('consent','update',{'analytics_storage':'granted'});" +
  "}catch(e){}})();";
