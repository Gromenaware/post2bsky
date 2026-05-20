# post2bsky 🦋

Una col·lecció d'eines Python per mirar contingut automàticament a [Bluesky](https://bsky.app), incloent publicacions de Twitter/X, feeds RSS i vídeos de TikTok. Dissenyat per executar-se com a tasques programades de CI/CD (GitHub Actions o Jenkins).

---

## 📦 Scripts Disponibles

Aquesta taula resumeix les eines principals incloses en el projecte i la seva funció.

| Script | Descripció |
|---|---|
| `twitter2bsky.py` | Miralla publicacions d'un compte de Twitter/X a Bluesky |
| `rss2bsky.py` | Publica elements d'un feed RSS a Bluesky |
| `twitter_login.py` | Extreu un token d'autenticació de Twitter mitjançant un navegador headless i desa un fitxer de sessió |
| `tiktok2bsky.py` | Scrapes vídeos recents d'un perfil públic de TikTok i els publica a Bluesky |

Aquests scripts formen el nucli de l'automatització del teu contingut cap a Bluesky.

---

## ⚙️ Requisits i Instal·lació

### Sistema

- **Python:** 3.10 o superior.
- **FFmpeg:** Necessari per a la llibreria `moviepy` (per al processament de vídeo).

### Dependències Python

Instal·la tots els paquets necessaris executant el següent comandament:

```bash
pip install -r requirements.txt
```

*(Les dependències inclouen: `atproto`, `tweety-ns`, `playwright`, `playwright-stealth`, `httpx`, `arrow`, `python-dotenv`, `moviepy`, `fastfeedparser`, `beautifulsoup4`, `charset-normalizer`, `Pillow` i `grapheme`)*

### Navegador Playwright

Després d'instal·lar les dependències, cal instal·lar el binari del navegador Chromium perquè el login headless funcioni:

```bash
python -m playwright install chromium
```

---

## 🔐 Configuració de Credencials

Els scripts requereixen les següents credencials, ja sigui com a variables d'entorn, un fitxer `.env` o secrets de CI/CD.

| Variable | Utilitzada per | Descripció |
|---|---|---|
| `TWITTER_USERNAME` | `twitter2bsky.py`, `twitter_login.py` | Nom d'usuari de Twitter/X |
| `TWITTER_PASSWORD` | `twitter2bsky.py`, `twitter_login.py` | Contrasenya de Twitter/X |
| `TWITTER_EMAIL` | `twitter2bsky.py` | Correu electrònic del compte de Twitter/X |
| `TWITTER_HANDLE` | `twitter2bsky.py` | Handle de Twitter/X a mirallar (p. ex. `elmeuhandle`) |
| `BSKY_HANDLE` | `twitter2bsky.py`, `rss2bsky.py`, `tiktok2bsky.py` | Handle de Bluesky (p. ex. `jo.bsky.social`) |
| `BSKY_USERNAME` | `rss2bsky.py` | Nom d'usuari de Bluesky |
| `BSKY_APP_PASSWORD` | `twitter2bsky.py`, `rss2bsky.py`, `tiktok2bsky.py` | Contrasenya d'aplicació de Bluesky |
| `TIKTOK_HANDLE` | `tiktok2bsky.py` | Handle del perfil públic de TikTok a mirallar |

**Important:** No facis mai commit de les credencials al repositori. Utilitza un fitxer `.env` localment (ja cobert per `.gitignore`) o secrets de CI/CD en producció.

---

## 🚀 Guia d'Ús

### 1. Obtenir un token de sessió de Twitter (`twitter_login.py`)

Executa això una sola vegada per autenticar-te i desar un fitxer de sessió reutilitzable:

```bash
python twitter_login.py 'EL_TEU_USUARI' 'LA_TEVA_CONTRASENYA'
```

⚠️ **Atenció:** Utilitza sempre cometes simples al voltant de la contrasenya per evitar que la shell interpreti caràcters especials com `!`, `$` o les cometes inverses.

En cas d'èxit, es crea un fitxer `my_account_session` al directori de treball. Els scripts posteriors reutilitzaran aquesta sessió sense necessitat de tornar a iniciar sessió. Si alguna cosa va malament, es desarà un `error_screenshot.png` al directori de treball perquè puguis veure exactament què ha trobat el navegador.

### 2. Mirallar Twitter a Bluesky (`twitter2bsky.py`)

Utilitza aquest script per sincronitzar les teves publicacions:

```bash
python twitter2bsky.py \
  --twitter-username "EL_TEU_USUARI" \
  --twitter-password "LA_TEVA_CONTRASENYA" \
  --twitter-email    "EL_TEU_CORREU" \
  --twitter-handle   "EL_TEU_HANDLE" \
  --bsky-handle      "EL_TEU_HANDLE.bsky.social" \
  --bsky-password    "LA_TEVA_APP_PASSWORD" \
  --bsky-base-url    "https://bsky.social" \
  --bsky-langs       "ca"
```

### 3. Publicar un feed RSS a Bluesky (`rss2bsky.py`)

Per enviar contingut des d'un RSS directament al teu perfil de Bluesky:

```bash
python rss2bsky.py \
  "https://example.com/feed.rss" \
  "EL_TEU_BSKY_HANDLE" \
  "EL_TEU_BSKY_USERNAME" \
  "LA_TEVA_APP_PASSWORD" \
  --service "https://bsky.social"
```

### 4. Mirallar TikTok a Bluesky (`tiktok2bsky.py`)

#### 🍪 Gestió de les cookies de TikTok

TikTok requereix cookies vàlides de sessió per poder accedir als perfils sense ser bloquejat. El fitxer `tiktok_cookies.json` conté aquestes cookies i **cal generar-lo manualment** seguint aquests passos:

1. **Obre el navegador** (Chrome o Firefox) i accedeix a [https://www.tiktok.com](https://www.tiktok.com).
2. **Inicia sessió** amb el teu compte de TikTok (o simplement navega fins que TikTok carregui correctament sense modals de cookies).
3. **Obre les DevTools** (`F12`) → pestanya **Application** (Chrome) o **Storage** (Firefox).
4. A la secció **Cookies → https://www.tiktok.com**, exporta totes les cookies.
5. Desa-les en format JSON com a `tiktok_cookies.json` al directori arrel del projecte.

Pots utilitzar extensions de navegador com [EditThisCookie](https://chrome.google.com/webstore/detail/editthiscookie) o [Cookie-Editor](https://cookie-editor.cgagnier.ca/) per exportar les cookies directament en format JSON compatible.

El fitxer ha de tenir aquest format:

```json
[
  {
    "name": "sessionid",
    "value": "el_teu_valor",
    "domain": ".tiktok.com",
    "path": "/",
    "httpOnly": true,
    "secure": true
  }
]
```

> ⚠️ **Les cookies de TikTok caduquen periòdicament.** Si el script deixa de funcionar o no troba vídeos, regenera el fitxer `tiktok_cookies.json` repetint el procés anterior.

> 🔒 **No facis mai commit del fitxer `tiktok_cookies.json`** al repositori. Ja està cobert pel `.gitignore`. En entorns CI/CD, injecta'l com a secret o artifact protegit.

#### ▶️ Execució de `tiktok2bsky.py`

Un cop tens el fitxer de cookies, executa el script així:

```bash
python tiktok2bsky.py \
  --tiktok-handle     "EL_HANDLE_DE_TIKTOK" \
  --bsky-handle       "EL_TEU_HANDLE.bsky.social" \
  --bsky-app-password "xxxx-xxxx-xxxx-xxxx" \
  --bsky-base-url     "https://bsky.social" \
  --bsky-langs        "ca" \
  --cookies-path      "tiktok_cookies.json"
```

| Paràmetre | Descripció |
|---|---|
| `--tiktok-handle` | Handle del perfil públic de TikTok (sense `@`) |
| `--bsky-handle` | Handle de Bluesky (p. ex. `jo.bsky.social`) |
| `--bsky-app-password` | Contrasenya d'aplicació de Bluesky |
| `--bsky-base-url` | URL base del PDS (per defecte: `https://bsky.social`) |
| `--bsky-langs` | Codi d'idioma de les publicacions (p. ex. `ca`, `es`, `en`) |
| `--cookies-path` | Ruta al fitxer JSON de cookies de TikTok (per defecte: `tiktok_cookies.json`) |

#### 📋 Comportament del script

- Scrapes els **darrers 30 vídeos** del perfil públic indicat.
- Filtra vídeos de **més de 3 dies d'antiguitat** i de **més de 179 segons** de durada (límit de Bluesky).
- Desa l'estat dels vídeos ja publicats a `tiktok2bsky_state.json` per evitar duplicats.
- Els logs es guarden a `tiktok2bsky.log`.
- Gestiona automàticament els **modals de cookies i banners** de TikTok mitjançant Playwright.
- Aplica **stealth mode** (si `playwright-stealth` v1.x és instal·lat) per reduir la detecció de bots.
- Límit de mida de vídeo: **20 MB** per a `bsky.social`, **10 MB** per a PDS de tercers.

---

## 🤖 Automatització (CI/CD)

Tots els scripts estan dissenyats per executar-se de forma programada sense intervenció manual.

### GitHub Actions

Els fitxers de workflow ja estan inclosos a la carpeta `.github/workflows/`.

| Fitxer de workflow | Script | Programació |
|---|---|---|
| `.github/workflows/twitter2bsky.yml` | `twitter2bsky.py` | Cada 30 minuts |
| `.github/workflows/rss2bsky.yml` | `rss2bsky.py` | Cada 30 minuts |
| `.github/workflows/tiktok2bsky.yml` | `tiktok2bsky.py` | Cada 30 minuts |

Recorda afegir els secrets necessaris a **Settings → Secrets and variables → Actions**.

### Jenkins

També es proporcionen `Jenkinsfiles` per a totes les pipelines. Tots utilitzen:

- Trigger cron `H/30 * * * *`
- `disableConcurrentBuilds()`
- Credencials injectades mitjançant `withCredentials`

---

## 🗂️ Estructura del Projecte

```
post2bsky/
├── twitter2bsky.py               # Script de mirroring Twitter → Bluesky
├── rss2bsky.py                   # Script de mirroring RSS → Bluesky
├── tiktok2bsky.py                # Script de mirroring TikTok → Bluesky
├── twitter_login.py              # Login headless de Twitter
├── tiktok_cookies.json           # Cookies de sessió de TikTok (NO fer commit!)
├── tiktok2bsky_state.json        # Estat dels vídeos publicats (generat automàticament)
├── requirements.txt
├── .env                          # Credencials locals (NO fer commit!)
├── .gitignore
├── .github/
│   └── workflows/
│       ├── twitter2bsky.yml
│       ├── rss2bsky.yml
│       └── tiktok2bsky.yml
└── Jenkinsfile
```

---

## 👤 Autor
[Guillem Hernández Sola](https://www.linkedin.com/in/guillemhs/)
[GitHub: @guillemhs](https://github.com/guillemhs)

## 📄 Llicència

Aquest codi és alliberat al domini públic per [Guillem Hernández Sola](https://www.linkedin.com/in/guillemhs/) sota llicència Creative Commons Reconeixement-NoComercial 4.0 Internacional (CC BY-NC 4.0).  

El README ha estat redactat per a aquest repositori i segueix la mateixa llicència.

Per a més informació o contacte, consulta el perfil de l’autor al repositori.

Si necessiteu més informació, podeu contactar amb [Guillem Hernández Sola](https://www.linkedin.com/in/guillemhs/).

## Contacte
Per dubtes o col·laboració: obre una issue al repositori o contacta amb en [Guillem Hernández Sola](https://www.linkedin.com/in/guillemhs/)