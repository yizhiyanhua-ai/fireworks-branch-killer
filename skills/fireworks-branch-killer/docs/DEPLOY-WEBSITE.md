# Standalone Website Deployment Guide (`https://getfbk.dev`)

The `site/` directory is a **zero-build static website** tailored for **Cloudflare Pages** with an independent custom domain (`https://getfbk.dev`)—completely independent of `github.io`.

## 1. Register `getfbk.dev` on Cloudflare Registrar (At-Cost)

1. Log in to the [Cloudflare Dashboard](https://dash.cloudflare.com/).
2. In the left sidebar, go to **Domain Registration → Register Domains**.
3. Search for **`getfbk.dev`** (verified available via Google Registry RDAP; standard `.dev` at-cost pricing ~$10–$12/yr + ICANN fee).
4. Complete checkout — Cloudflare automatically provisions authoritative DNS and DNSSEC in your account.

## 2. Deploy `site/` to Cloudflare Pages

1. In Cloudflare Dashboard, go to **Workers & Pages → Create → Pages → Connect to Git**.
2. Select repository **`yizhiyanhua-ai/fireworks-branch-killer`**.
3. Configure build settings:
   - **Production branch**: `main`
   - **Framework preset**: `None`
   - **Build command**: *(leave blank)*
   - **Build output directory**: `site`
4. Click **Save and Deploy**. Every push to `main` touching `site/` automatically updates production globally with `_headers` security rules applied.

## 3. Bind `getfbk.dev` & `www.getfbk.dev`

1. Open your newly deployed Cloudflare Pages project → **Custom domains → Set up a custom domain**.
2. Enter **`getfbk.dev`** and confirm — Cloudflare creates the apex CNAME flattening record and provisions a Universal SSL certificate automatically within ~30 seconds.
3. Optional: Also add `www.getfbk.dev` and configure a Redirect Rule (`www.getfbk.dev/*` → `https://getfbk.dev/$1` HTTP 301) so canonical SEO remains unified on `https://getfbk.dev/`.
