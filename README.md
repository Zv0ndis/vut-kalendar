# vut-kalendar

Rozvrh, zkoušky, termíny odevzdání a rezervace místností z VUT Studisu se samy
synchronizují do tvého Google Kalendáře. Běží zdarma na GitHub Actions ve tvém
vlastním soukromém repozitáři, nic nemusíš nechávat zapnuté.

Volitelně k tomu můžeš přidat Discord bota (ZHelper), který ti píše denní
přehled, hlídá registrace zkoušek, volná místa a deadliny.

## Obsah

1. [Co to umí](#co-to-umí)
2. [Co potřebuješ](#co-potřebuješ)
3. [Část 1: synchronizace do Google Kalendáře](#část-1-synchronizace-do-google-kalendáře)
4. [Část 2: Discord bot ZHelper (volitelné)](#část-2-discord-bot-zhelper-volitelné)
5. [Aktualizace ze šablony](#aktualizace-ze-šablony)
6. [Řešení potíží](#řešení-potíží)
7. [Soukromí a bezpečnost](#soukromí-a-bezpečnost)
8. [FAQ](#faq)

## Co to umí

Do zvláštního kalendáře (výchozí název **Škola**) se dostane:

- rozvrh z VUT Studisu,
- přihlášené termíny zkoušek,
- termíny odevzdání projektů (ze Studisu a volitelně z Moodle),
- rezervace místností.

Barvy podle typu:

- modrá: přednáška
- zelená: cvičení / laborka
- červená: zkouška
- oranžová: deadline
- šedá: rezervace
- levandulová: blok na učení

Další vlastnosti:

- Přednášky se v kalendáři tváří jako volný čas.
- Výuka ve dnech státních svátků se vyhodí.
- Zkoušky mají česky popsané body: maximum, minimum, kolik už máš a kolik ještě
  můžeš získat, plus jméno vyučujícího.
- Před zkouškami vznikají bloky **Učení:**, jejich délka se řídí tím, kolik bodů
  je ve hře.
- U místností na FIT je odkaz na stránku místnosti.
- Placeholdery typu "zápočet" se schovají.
- Tvoje vlastní poznámky v popisu události zůstanou zachované, pokud je
  napíšeš **nad** řádek `— Studis —`. Vše pod ním se přepisuje.
- Změna místnosti nebo přesunutá hodina upraví existující událost, nevznikne
  duplicita. Případné duplicity se uklidí.
- Události, které jsi do kalendáře vytvořil ručně, skript nikdy nesahá.
- **Pojistka proti hromadnému mazání:** pokud by běh chtěl smazat víc než 30 %
  událostí, nesmaže nic (a pokud máš bota, pošle ti zprávu). Přebít to jde
  přepínačem `--force`.
- `--dry-run` jen vypíše, co by se stalo, a nic nezapíše.

## Co potřebuješ

- účet na GitHubu a nainstalovaný `git`,
- VUT login a heslo (to samé, co používáš do Studisu),
- Google účet,
- počítač s prohlížečem, na kterém jednorázově proběhne přihlášení ke Googlu,
  a na něm [uv](https://docs.astral.sh/uv/),
- asi 30 minut času.

Volitelné:

- API klient VUT IS (rychlejší a stabilnější data, viz krok 4),
- odkaz na export z Moodle,
- pro bota: Discord účet a stále zapnutý Linux (viz část 2).

Cena: nula. Soukromé repozitáře mají na GitHub Actions 2000 minut měsíčně
zdarma, což pro dva běhy denně bohatě stačí.

Funguje pro všechny fakulty VUT. Odkazy na místnosti jsou jen pro FIT; bez
VUT API mohou u jiných fakult chybět data o obdobích semestru, což nevadí.

### Jak se přihlásit ke GitHubu při klonování

Tvůj repozitář je **soukromý**, takže `git clone` si řekne o přihlášení a
GitHub už nepřijímá heslo. Nejjednodušší je:

1. Nainstaluj [GitHub CLI](https://cli.github.com/).
2. Spusť `gh auth login`, vyber GitHub.com, HTTPS a přihlas se přes prohlížeč.

Alternativa: vytvoř si Personal access token (GitHub → Settings → Developer
settings → Personal access tokens) a při dotazu na heslo zadej token místo
hesla. Token potřebuje scope `repo`, a `workflow`, pokud budeš tlačit změny v
`.github/workflows` (viz [Aktualizace](#aktualizace-ze-šablony)).

## Část 1: synchronizace do Google Kalendáře

### 1. Vytvoř si vlastní repozitář ze šablony

1. Otevři repozitář šablony (odkaz ti poslal kamarád).
2. Klikni na zelené tlačítko **Use this template** → **Create a new repository**.
3. Zvol vlastní jméno (třeba `vut-kalendar`) a **Private**. Nikdy ne Public.
4. Klikni na **Create repository**.

Všechno další děláš ve svém novém repozitáři.

### 2. Google Cloud: projekt a Calendar API

1. Jdi na <https://console.cloud.google.com/> a přihlas se svým Google účtem.
2. Nahoře vlevo klikni na výběr projektu → **New project**. Název třeba
   `vut-kalendar`, **Create**. Pak ho vyber.
3. V menu **APIs & Services** → **Library** vyhledej **Google Calendar API**
   a klikni na **Enable**.

### 3. Google Cloud: souhlasová obrazovka, publikace a klíč

Pořadí je důležité: nejdřív souhlasová obrazovka, pak **publikace**, pak klíč
a až potom přihlášení.

**a) Souhlasová obrazovka**

1. **APIs & Services** → **OAuth consent screen**. V novějším rozhraní je to
   **Google Auth Platform**; pokud ho vidíš poprvé, klikni na **Get started**.
2. Vyplň název aplikace (cokoli) a svůj e-mail jako podporu i kontakt.
3. Typ uživatelů (Audience): **External**.
4. Dokonči a ulož. Žádné scopes ručně přidávat nemusíš (v **Data Access**
   nech prázdné), skript si potřebný přístup vyžádá sám při přihlášení.
   Testovacího uživatele přidávat nemusíš.

**b) Publikuj aplikaci (nejdůležitější krok)**

V režimu *Testing* Google po 7 dnech zneplatní přihlašovací token a
synchronizace by tiše přestala fungovat.

1. Na stránce **Audience** (starší UI: **OAuth consent screen**) klikni na
   **Publish app** → **Confirm**.
2. Zkontroluj, že stav je **In production**. Pokud tam stále stojí
   **Testing**, token ti za 7 dní umře.

Aplikace zůstane "neověřená". To je v pořádku, je jen pro tebe. Při
přihlášení uvidíš varování; klikni na **Advanced** (Rozšířené) →
**Go to ... (unsafe)** a pokračuj.

**c) Klíč (OAuth client)**

1. Nové UI: **Google Auth Platform** → **Clients** → **Create client**.
   Staré UI: **APIs & Services** → **Credentials** → **Create credentials**
   → **OAuth client ID**.
2. Application type: **Desktop app**, název libovolný, **Create**.
3. Klikni na **Download JSON**.
4. Stažený soubor se často jmenuje
   `client_secret_<dlouhé-id>.apps.googleusercontent.com.json`. **Přejmenuj ho
   na `client_secret.json`.**

### 4. (Volitelné) Oficiální VUT API

Bez API se všechno stahuje přes headless prohlížeč ze Studisu. Funguje to,
ale s API je to rychlejší a odolnější. Pokud ho nechceš, přeskoč na krok 5.

1. Otevři <https://id.vut.cz/my-account/client/> a přihlas se.
2. Klikni na **Přidat**, typ **OAuth2 Client Credentials**, prostředí
   **produkce**.
3. U vytvořeného klienta si opiš **Klient UID**. To je tvoje `VUT_API_CLIENT_ID`.
4. V sekci **Client secrets** klikni na **Přidat** a nastav datum expirace.
5. **Secret se zobrazí jen jednou.** Hned ho zkopíruj, to je `VUT_API_SECRET`.
   Nemusíš zaškrtávat žádné scopes ani oprávnění.
6. **Dej si do kalendáře připomínku na den expirace.** Po vypršení secretu se
   všechno bez varování vrátí ke stahování přes prohlížeč. Pak vytvoř nový
   secret a aktualizuj ho v GitHubu (a v `.env` na serveru).

Limit API je 10 tokenů za hodinu; skripty ho hlídají, normální provoz ho
nepřekročí.

### 5. (Volitelné) Moodle

Pokud chceš deadliny z Moodle:

1. Otevři <https://moodle.vut.cz/>, přejdi na **Kalendář** → **Exportovat**.
2. Zvol, co chceš exportovat, a **Získat URL kalendáře**.
3. Zkopíruj odkaz, použiješ ho jako `MOODLE_ICS_URL`.

Ten odkaz je jako heslo: kdo ho má, vidí tvůj kalendář.

### 6. Jednorázové přihlášení ke Googlu (token.json)

Dělej to na počítači, který má **prohlížeč** (ne přes SSH na serveru):

1. Nainstaluj [uv](https://docs.astral.sh/uv/) (návod na jeho stránce).
2. Naklonuj svůj repozitář a vlez do něj (nahraď `TVUJ-LOGIN`):

```bash
git clone https://github.com/TVUJ-LOGIN/vut-kalendar.git
cd vut-kalendar
```

3. Vlož `client_secret.json` do **kořenové složky repozitáře**, hned vedle
   `sync.py`.
4. Spusť přihlášení (až po publikaci aplikace, krok 3b):

```bash
uv run sync.py --login
```

5. Otevře se prohlížeč. Přihlas se, přes varování "aplikace není ověřená"
   klikni **Advanced** → pokračovat a povol přístup ke kalendáři.
6. Úspěch poznáš podle hlášky `Přihlášení hotovo. Obsah souboru ... vlož do
   tajného klíče GOOGLE_TOKEN.` a podle toho, že vedle `sync.py` vznikl soubor
   `token.json`.

Soubory `client_secret.json` a `token.json` jsou v `.gitignore`, přesto je
**nikdy nenahrávej na GitHub**, nikomu je neposílej a nevkládej do chatů.

### 7. Tajné klíče v GitHubu (secrets)

Ve svém repozitáři: **Settings** → **Secrets and variables** → **Actions** →
**New repository secret**. Pro každý řádek vytvoř jeden secret (název přesně
jak je napsáno):

| Název | Hodnota | Povinné |
|---|---|---|
| `GOOGLE_CLIENT_SECRET` | celý obsah `client_secret.json` | ano |
| `GOOGLE_TOKEN` | celý obsah `token.json` | ano |
| `VUT_USER` | tvůj VUT login | ano |
| `VUT_PASS` | tvoje VUT heslo | ano |
| `VUT_API_CLIENT_ID` | Klient UID z kroku 4 | ne |
| `VUT_API_SECRET` | secret z kroku 4 | ne |
| `MOODLE_ICS_URL` | odkaz z kroku 5 | ne |

Obsah souborů zkopíruj celý: otevři soubor v textovém editoru (na Windows třeba
v Poznámkovém bloku) nebo na Linuxu/macOS použij `cat token.json`. Vlož ho
**tak, jak je**, jako čisté JSON (klidně na víc řádků), bez uvozovek navíc a bez
kódování do base64.

Nepovinné nastavení (stejným způsobem jako secret, případně jako proměnná v **Variables**):

| Název | Co dělá |
|---|---|
| `CALENDAR_NAME` | název kalendáře, výchozí `Škola` |
| `SKIP_TITLES` | regulární výraz pro názvy událostí, které nechceš |
| `ROOM_URL` | šablona odkazu na místnost (`{}` = místnost); výchozí je FIT, hodnota `off` odkazy vypne |

### 8. První spuštění

1. V repozitáři otevři záložku **Actions**. Pokud GitHub zobrazí hlášení o
   workflows, potvrď **I understand my workflows, enable them**.
2. Vyber workflow **sync**, klikni **Run workflow** → **Run workflow**.
3. Po minutě dvou by měl být běh zelený. (Chybí-li některý povinný secret, běh
   hned na začátku skončí s hláškou, který to je.) Otevři ho a v kroku **sync to Google
   Calendar** najdeš řádek `google: N before; keep ..., patch ..., delete ...,
   insert ...`. Při prvním běhu by mělo být hodně `insert`.
4. V Google Kalendáři se v levém panelu objeví kalendář **Škola**. Může to
   trvat několik minut. V mobilní aplikaci Google Kalendář ho případně musíš
   zapnout v nastavení (zaškrtnout kalendář **Škola**).

Dál se to spouští samo **dvakrát denně**. Kdykoli můžeš spustit ručně přes
**Run workflow**. Pokud je běh červený, viz [Řešení potíží](#řešení-potíží).

Skript spravuje jen události, které sám vytvořil (označuje si je). Tvoje ruční
události v tom kalendáři se nezmění ani nesmažou, ať se jmenují jakkoli.

**Nepovinné: suchý běh na svém počítači.** `uv run sync.py --dry-run` sám data
nestahuje. Nejdřív potřebuješ lokální `.env` s `VUT_USER` a `VUT_PASS` (podle
`.env.example`) a pak:

```bash
uv run fetch_studis.py --install-browser
uv run fetch_studis.py
uv run sync.py --dry-run
```

Poslední příkaz vypíše plán a nic nezapíše. Pokud ti to připadá složité, tenhle
krok klidně přeskoč.

## Část 2: Discord bot ZHelper (volitelné)

Bot ti posílá zprávy do Discordu (soukromé zprávy jen tobě):

- ranní přehled dne,
- večerní připomínka ve 20:00,
- nedělní týdenní přehled,
- upozornění na otevření a uzavření registrace zkoušky a na uvolněná místa,
- připomínky deadlinů,
- nové body,
- slash příkazy.

Volitelně i chat s AI přes Anthropic API klíč, s denním limitem útraty
(výchozí 1 USD, nastavitelné přes `ZHELPER_DAILY_USD`).

Bot potřebuje stroj, který běží pořád, s Linuxem a systemd: Oracle Cloud
Always Free VM, Raspberry Pi nebo domácí server. Ozkoušeno na Ubuntu (včetně
Raspberry Pi OS 64-bit); jiné distribuce se systemd by měly fungovat.
U Oracle Cloud vol **Ampere A1**; 1 GB stroj E2.1.Micro je na Chromium
těsný. Bez bota synchronizace funguje dál na GitHub Actions.

Požadavky na stroji: `git`, `curl`, `python3`, `flock` (balík util-linux) a
`sudo`. Všechno spouštěj jako **běžný uživatel, ne root**.

### A. Discord aplikace a bot

1. Mít (nebo vytvořit) **vlastní Discord server**, třeba jen pro tebe.
2. Otevři <https://discord.com/developers/applications> → **New Application**.
3. Vlevo **Bot** → **Reset Token** a token zkopíruj. To je `DISCORD_BOT_TOKEN`,
   tajné jako heslo. Na stejné stránce **vypni Public Bot**, ať si bota nemůže
   přidat někdo cizí.
4. Žádné privilegované intenty zapínat nemusíš (bot čte jen soukromé zprávy).
5. **OAuth2** → **URL Generator** (případně záložka **Installation**): scope
   `bot` a `applications.commands`, žádná oprávnění. Vygenerovaný odkaz otevři
   a přidej bota na svůj server. Discord píše soukromé zprávy jen lidem, se
   kterými bot sdílí server.
6. Na svém serveru: klikni na jeho název → **Nastavení soukromí** a zapni
   **Povolit přímé zprávy od členů serveru** (Allow direct messages from
   server members). Jinak ti bot nemůže psát.
7. V Discordu zapni **Nastavení** → **Pokročilé** → **Vývojářský režim**,
   pravým tlačítkem na sebe → **Kopírovat ID uživatele**. To je
   `DISCORD_USER_ID`.

### B. Server

Bot na serveru sám stahuje data a synchronizuje kalendář (systemd časovače),
takže server potřebuje stejné přihlašovací soubory jako GitHub Actions.
Podrobnosti jsou v `deploy/README-bot.md`.

1. Připoj se přes SSH a nainstaluj [uv](https://docs.astral.sh/uv/) (plus `git`, `curl`, `python3`,
   `flock`, `sudo`).
2. Naklonuj svůj soukromý repozitář. Server potřebuje vlastní přihlášení k
   GitHubu: buď `gh auth login`, nebo deploy key (repozitář → **Settings** →
   **Deploy keys**), nebo token jako na počítači.

```bash
git clone https://github.com/TVUJ-LOGIN/vut-kalendar.git
cd vut-kalendar
```

3. Zkopíruj `client_secret.json` a `token.json` z kroku 6 do **kořene**
   repozitáře na serveru, např.
   `scp client_secret.json token.json uzivatel@server:vut-kalendar/`.
4. Vytvoř `.env` podle `.env.example` a `deploy/bot.env.example`, minimálně:

```
VUT_USER=...
VUT_PASS=...
DISCORD_USER_ID=...
```

   Komentáře v `.env` patří **na vlastní řádek**, nikdy za hodnotu. Další
   volitelné proměnné: `VUT_API_CLIENT_ID` a `VUT_API_SECRET` (nebo skript
   `deploy/set-vut-api.sh`), `DIGEST_CALENDARS` (další kalendáře v ranním
   přehledu, `primary` = hlavní kalendář), `QUIET_FROM` / `QUIET_TO` (tiché
   hodiny, výchozí 22 a 8), `ANTHROPIC_API_KEY`, `ZHELPER_DAILY_USD`.
5. Uložení tokenu bota, registrace příkazů a instalace služeb:

```bash
deploy/set-token.sh             # zeptá se na token bota (skrytě), zapíše do .env
deploy/register-commands.sh     # zaregistruje slash příkazy
deploy/install.sh               # systemd jednotky, časovače, prohlížeč; chce sudo
sudo systemctl enable --now zhelper
```

   `install.sh` doinstaluje i prohlížeč (jen na distribucích s apt).
6. Pošli botovi soukromou zprávu nebo zkus `/dnes`. Logy: `journalctl -u zhelper`.

Na Fedoře (SELinux) může služba skončit chybou `203/EXEC`, protože `uv` leží
v domovské složce. Nainstaluj `uv` systémově (např. do `/usr/local/bin`) nebo
mu změň štítek: `chcon -t bin_t $(command -v uv)`.

`install.sh` zapne časovače jen tehdy, když je v `.env` `DISCORD_BOT_TOKEN`;
jinak to vypíše a stačí po `set-token.sh` spustit `install.sh` znovu.

Časovače: každých 5 minut kontrola upozornění, každou hodinu obnova přehledu,
dvakrát denně stažení a sync (05:15 a 16:15 UTC), večerní přehled ve 20:00
a nedělní týdenní přehled.

Protože sync teď běží i na serveru, GitHub workflow **sync** už
nepotřebuješ: v záložce **Actions** ho vlevo vyber a přes **...** → **Disable
workflow**. Nebo ho nech běžet jako zálohu, dvojí sync nevadí.

Slash příkazy: `/dnes`, `/zitra`, `/tyden`, `/sync`, `/body`, `/terminy`,
`/stav`, `/ucit`, `/motd` a `/ai` (chat jen s nastaveným `ANTHROPIC_API_KEY`).

## Aktualizace ze šablony

Repozitář vytvořený ze šablony s ní nemá společnou historii, takže se nové verze
přenášejí jednoduše: přepíšeš kódové soubory novými. Tvoje data a klíče jsou jen
v GitHub secrets a v `.env`, do repozitáře nepatří, takže o ně nepřijdeš.

```bash
git remote add template https://github.com/Zv0ndis/vut-kalendar.git   # jen poprvé
git fetch template
git diff --stat HEAD template/main                              # co se změnilo
git checkout template/main -- .                                 # přepíše soubory novou verzí
git add -A && git commit -m "aktualizace ze šablony" && git push
```

Aktualizace **přepíše také `README.md` a `.gitignore`**. Pokud jsi kód sám upravoval, před `checkout` si změny zazálohuj; přepíšou se.
Nové soubory se přidají, smazané ze šablony ti zůstanou (můžeš je smazat ručně).

Pokud aktualizace mění soubory v `.github/workflows`, push přes HTTPS vyžaduje
token se scope `workflow` (u `gh auth login` ho dostaneš přidáním scope:
`gh auth refresh -s workflow`).

Máš-li i bota na serveru: tam jen `git pull`, a když se změnily soubory v `deploy/`,
spusť znovu `deploy/install.sh`; potom `sudo systemctl restart zhelper`.
Když novější verze potřebuje další secret nebo proměnnou v `.env`, napíše se to
do změn v `.env.example` (porovnej `git diff HEAD template/main -- .env.example`).

## Řešení potíží

### Google token vypršel

Příznak: běh je červený a v logu stojí `token.json invalid or missing`.

1. Na svém počítači znovu spusť `uv run sync.py --login`.
2. Nový obsah `token.json` vlož do secretu `GOOGLE_TOKEN`.

Pokud se to opakuje po týdnu, nemáš aplikaci publikovanou, viz krok 3.

### Pojistka proti mazání

Log obsahuje `BRAKE: ...`. Skript by smazal víc než 30 % událostí nebo je
zdroj prázdný. Bez nastaveného Discordu v takovém případě běh na Actions
**skončí červeně**, s botem ti přijde zpráva. Obvykle je chyba ve zdroji (výpadek Studisu), ne v kalendáři.
Zkus běh za chvíli znovu. Jen když víš, že mazání je správně, spusť s
`--force`.

### Prolomení pojistky přes GitHub

Po velké legitimní změně (např. nový semestr): **Actions** → **sync** →
**Run workflow**, zaškrtni **force** a spusť. Tím se pojistka přeskočí.

### Přihlášení do Studisu selhává

Zkontroluj `VUT_USER` a `VUT_PASS` (po změně hesla je potřeba secret
aktualizovat). Pokud máš ve VUT zapnuté dvoufaktorové ověření, skript se
nemusí přihlásit; napiš tomu, kdo ti šablonu poslal.

### Kvóta API

Chyba o tokenech znamená vyčerpaných 10 tokenů za hodinu. Počkej hodinu,
nespouštěj víc běhů najednou.

### V kalendáři jsou duplicity

Typicky z dřívějšího ručního importu `.ics` do hlavního kalendáře. Jednorázově
spusť `uv run sync.py --adopt` (přesune kurzy z hlavního kalendáře do
**Škola** a uklidí duplicity); předtím zkus `--dry-run`.

### Actions běh je červený

1. **Actions** → klikni na červený běh.
2. Otevři krok, který selhal, a přečti poslední řádky.
3. Nejčastěji: vypršelý Google token, špatné heslo, nebo chybějící secret.

## Soukromí a bezpečnost

- Všechna data zůstávají v tvém soukromém repozitáři a na tvém Google účtu.
- Repozitář musí být **Private**.
- Nikdy nenahrávej `.env`, `client_secret.json`, `token.json` ani
  `.vut-token.json`.
- Secrets nikomu neposílej, ani kamarádovi, který ti šablonu dal.
- Pokud secret uniknul, zruš ho (Google: zrušit přístup ve správě účtu; VUT:
  smazat client secret; Discord: Reset Token) a vytvoř nový.

## FAQ

**Musím mít počítač pořád zapnutý?** Ne. Synchronizace běží na GitHubu.
Počítač potřebuješ jen jednou na přihlášení. Pro bota potřebuješ server.

**Kolik to stojí?** Nic. Bot s AI chatem stojí jen tolik, kolik nastavíš
limitem.

**Smaže to moje ruční události?** Ne, skript maže jen události tvaru, který
sám vytváří.

**Mohu si kalendář sdílet?** Ano, jako každý Google kalendář.

**Změnil se název předmětu / rozvrh?** Při dalším běhu se upraví existující
události.
