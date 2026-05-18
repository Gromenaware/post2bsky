# post2bsky 🦋

Una col·lecció d'eines Python per mirar contingut automàticament a [Bluesky](https://bsky.app), incloent publicacions de Twitter/X i feeds RSS. Dissenyat per executar-se com a tasques programades de CI/CD (GitHub Actions o Jenkins).

---

## 📦 Scripts Disponibles

Aquesta taula resumeix les eines principals incloses en el projecte i la seva funció.

| Script | Descripció |
|---|---|
| `twitter2bsky.py` | Miralla publicacions d'un compte de Twitter/X a Bluesky |
| `rss2bsky.py` | Publica elements d'un feed RSS a Bluesky |
| `twitter_login.py` | Extreu un token d'autenticació de Twitter mitjançant un navegador headless i desa un fitxer de sessió |

Aquests tres scripts formen el nucli de l'automatització del teu contingut cap a Bluesky.

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

*(Les dependències inclouen: `atproto`, `tweety-ns`, `playwright`, `httpx`, `arrow`, `python-dotenv`, `moviepy`, `fastfeedparser`, `beautifulsoup4`, `charset-normalizer`, `Pillow` i `grapheme`)*

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
| `TWITTER_HANDLE` | `twitter2bsky.py` | Handle de Twitter/X a mirallar (p. ex. elmeuhandle) |
| `BSKY_HANDLE` | `twitter2bsky.py`, `rss2bsky.py` | Handle de Bluesky (p. ex. jo.bsky.social) |
| `BSKY_USERNAME` | `rss2bsky.py` | Nom d'usuari de Bluesky |
| `BSKY_APP_PASSWORD` | `twitter2bsky.py`, `rss2bsky.py` | Contrasenya d'aplicació de Bluesky |

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

---

## 🤖 Automatització (CI/CD)

Tant `twitter2bsky.py` com `rss2bsky.py` estan dissenyats per executar-se de forma programada sense intervenció manual.

### GitHub Actions
Els fitxers de workflow ja estan inclosos a la carpeta `.github/workflows/`.

| Fitxer de workflow | Script | Programació |
|---|---|---|
| `.github/workflows/twitter2bsky.yml` | `twitter2bsky.py` | Cada 30 minuts |
| `.github/workflows/rss2bsky.yml` | `rss2bsky.py` | Cada 30 minuts |

*Recorda afegir els secrets necessaris a **Settings → Secrets and variables → Actions**.*

### Jenkins
També es proporcionen `Jenkinsfiles` per a ambdues pipelines. Tots dos utilitzen:
- Trigger cron `H/30 * * * *`
- `disableConcurrentBuilds()`
- Credencials injectades mitjançant `withCredentials`

---

## 📁 Estructura del Projecte

```text
post2bsky/
├── twitter2bsky.py               # Script de mirroring Twitter → Bluesky
├── rss2bsky.py                   # Script de publicació RSS → Bluesky
├── twitter_login.py              # Eina de login headless i sessió de Twitter
├── requirements.txt              # Dependències Python
├── .env.example                  # Exemple de fitxer de variables d'entorn
├── .github/
│   └── workflows/
│       ├── twitter2bsky.yml      # Workflow de GitHub Actions
│       └── rss2bsky.yml          # Workflow de GitHub Actions
├── Jenkinsfile.twitter2bsky      # Pipeline de Jenkins
└── Jenkinsfile.rss2bsky          # Pipeline de Jenkins
```

---

## 🛠️ Resolució de Problemes

Aquí tens les solucions als problemes més comuns que et pots trobar durant l'execució.

| Problema | Causa | Solució |
|---|---|---|
| La contrasenya es deforma al terminal | La shell expandeix `!` o `$` | Utilitza cometes simples: `'la!meva!contrasenya'` |
| Es crea `error_screenshot.png` | El flux de login ha fallat o s'ha detectat un bot | Obre la captura de pantalla per inspeccionar què ha mostrat X |
| No es troba `auth_token` | S'ha activat la verificació en 2 passos o per correu | Completa el repte manualment una vegada i torna-ho a intentar |
| `TimeoutError` a Playwright | X ha canviat el HTML de la pàgina de login | Actualitza els selectors a `twitter_login.py` |
| `ffmpeg: command not found` | FFmpeg no està instal·lat | `brew install ffmpeg` / `apt install ffmpeg` |
| Publicacions duplicades a Bluesky | L'script no té estat de deduplicació | Assegura't que només s'executa una instància alhora (bloqueig de concurrència) |

Amb aquesta taula hauries de poder diagnosticar i resoldre ràpidament qualsevol incidència habitual.

---

## 📄 Llicència
GNU GENERAL PUBLIC LICENSE
Version 3, 29 June 2007
## Autor
Guillem Hernández Sola