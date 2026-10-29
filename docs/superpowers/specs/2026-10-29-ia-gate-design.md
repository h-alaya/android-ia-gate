# IA Gate — Spécification de design (v1)

- **Date :** 2026-10-29
- **Statut :** à valider
- **Plan d'implémentation :** `docs/superpowers/plans/2026-10-29-ia-gate-v1.md`

## 1. Objectif

Permettre à Claude (Claude Code, Claude Desktop, et plus tard claude.ai) de piloter des
équipements Android métier (terminaux, valideurs — mode développeur modifié) pour
**installer et tester des applications**, en désignant l'équipement par un **identifiant
unique stable** :

> « Installe `app-debug.apk` sur **VAL-042** et lance les tests instrumentés. »

### Critères de succès

1. Depuis Claude Code ou Claude Desktop, sur le réseau privé, l'agent liste les équipements,
   en enrôle un nouveau, installe un APK, lance l'app, récupère logs / capture d'écran /
   résultats de tests — sur l'équipement nommé, sans jamais se tromper d'appareil.
2. Un équipement garde son identifiant après redémarrage, changement d'IP ou passage
   USB ↔ Wi-Fi.
3. Rien n'est exposé hors du réseau privé ; aucune app n'est requise sur l'équipement.

### Ce qui a été dit / ce qui est supposé

| Dit par l'utilisateur | Supposé (à corriger si faux) |
|---|---|
| Clients : Claude Code, Claude Desktop, claude.ai | v1 = Claude Code + Desktop ; claude.ai = phase 2 |
| Équipements Android génériques, mode dev modifié | adb accessible en USB **et/ou** TCP (`adb connect ip:5555`) |
| Un PC middleware peut parler aux équipements en adb | Le PC a `adb` (platform-tools) installé |
| PC en réseau privé, non exposé | Les APK sont présents sur le PC de la gate (build local, Jenkins, dossier partagé) |

## 2. Hors périmètre v1 (YAGNI)

- Accès depuis claude.ai (nécessite tunnel sortant + OAuth) → phase 2, le design le permet.
- App compagnon sur l'équipement (accès SDK matériel : imprimante, NFC…) → phase 3.
- Plusieurs gates fédérées, interface web, gestion de flotte / MDM.
- Upload d'APK via MCP (le fichier doit être sur le PC de la gate).

## 3. Architecture

```
 Claude Code ─┐                              ┌─ VAL-042 (USB)
              │  MCP Streamable HTTP          │
 Claude Desk ─┼─────────────────▶ IA Gate ───adb──┼─ VAL-043 (Wi-Fi 192.168.1.50:5555)
   (stdio ou  │  http://<pc>:8765/mcp  (PC)   │
   mcp-remote)┘  + Bearer token               └─ (non enrôlé) → enroll_device
```

- **IA Gate** = un seul processus Python : serveur MCP + registre d'équipements + client adb.
- **Transport** : `streamable-http` (mode démon, recommandé, un seul processus partagé) ou
  `stdio` (Claude Desktop lance la gate lui-même).
- **Gate → équipements** : binaire `adb` du PC (serveur adb local, port 5037).

### Stack

- Python ≥ 3.11, SDK MCP officiel `mcp>=2.3,<3` (`from mcp.server.mcpserver import MCPServer`),
  `uvicorn` pour le HTTP, `tomllib` (stdlib) pour la config, JSON pour le registre.
- Outillage : `uv`, `pytest` + `pytest-asyncio`, `ruff`.
- Justification : SDK MCP le plus mature, transports stdio + HTTP intégrés, client MCP
  en mémoire pour les tests, packaging simple (`uv tool install`). La phase 3 (app
  compagnon) sera en Kotlin dans `companion-app/`.

## 4. Identifiant unique des équipements

`ro.serialno` n'est pas fiable sur des ROM modifiées (vide, `unknown`,
`0123456789ABCDEF`, identique sur un lot). On utilise un **enrôlement** :

1. **Empreinte** lue via adb : `ro.serialno`, `android_id` (`settings get secure android_id`),
   `ro.product.manufacturer`, `ro.product.model`.
2. **ID gate** choisi à l'enrôlement : format `^[A-Z0-9][A-Z0-9_-]{1,31}$`, normalisé en
   majuscules, comparaison insensible à la casse (ex. `VAL-042`).
3. **Marqueur** écrit sur l'équipement : `/data/local/tmp/.ia-gate-id` contient l'ID
   (inscriptible par l'utilisateur `shell`, survit aux redémarrages, effacé par un reset usine).
4. **Résolution** d'un appareil connecté vers un ID, dans cet ordre :
   1. marqueur présent **et** connu du registre → cet ID, sauf si l'enregistrement contredit
      l'empreinte (deux `serialno` valides différents, ou deux `android_id` connus différents :
      marqueur périmé d'un appareil retiré ou réattribué) : on passe alors au critère suivant ;
   2. sinon `serialno` *valide* correspondant à exactement un enregistrement → cet ID, sauf si
      les deux `android_id` sont connus et diffèrent (frère d'un lot partageant le serial, ou
      appareil réinitialisé) : on passe alors au critère suivant ;
   3. sinon `android_id` correspondant à exactement un enregistrement → cet ID, sauf si deux
      `serialno` réels (ni vides ni factices, **même s'ils sont partagés** par des appareils
      connectés) diffèrent (image ROM clonée partageant l'`android_id`) : on passe alors au
      critère suivant ;
   4. sinon : non enrôlé.

   Un `serialno` est **invalide** s'il est vide, `unknown`, `0123456789ABCDEF`, `0123456789`,
   ou partagé par au moins deux appareils connectés.

   Un appareil réinitialisé (nouvel `android_id`, marqueur effacé) apparaît donc non enrôlé.
   Le registre ne peut pas le distinguer d'un frère de lot partageant le `serialno` : son
   ancien ID reste attribué, et pour le ré-enrôler sous cet ID il faut d'abord retirer l'ID
   du registre.
5. Le **serial adb** (`R58M…` en USB, `ip:port` en Wi-Fi) n'est jamais un identifiant : il
   sert seulement à adresser la commande adb une fois l'ID résolu.

Registre : `~/.ia-gate/devices.json`, écrit de façon atomique (fichier temporaire +
`os.replace`), relu à chaque résolution (supporte plusieurs processus en lecture).

## 5. Tools MCP (v1)

Chaque tool qui vise un équipement prend `device_id` (l'ID gate). Les opérations sur un même
équipement sont **sérialisées** (verrou par équipement) ; des équipements différents sont
traités en parallèle.

| Tool | Rôle |
|---|---|
| `list_devices()` | équipements enrôlés + connectés, état (`online`/`offline`/`unauthorized`/`disconnected`), et appareils connectés non enrôlés (avec leur serial adb) |
| `enroll_device(adb_serial, device_id, label="")` | enrôle un appareil connecté, écrit le marqueur |
| `device_info(device_id)` | empreinte, version Android, batterie, résolution écran |
| `connect_wifi(host, port=5555)` | `adb connect` |
| `install_apk(device_id, apk_path, replace=True)` | installe un APK situé dans un dossier autorisé du PC |
| `uninstall_app(device_id, package)` | désinstalle |
| `launch_app(device_id, package, activity=None)` | démarre l'app, renvoie son PID |
| `stop_app(device_id, package)` | `am force-stop` |
| `get_logs(device_id, package=None, lines=500)` | logcat (filtré par PID si `package`), tronqué à `max_log_bytes` |
| `clear_logs(device_id)` | `logcat -c` |
| `screenshot(device_id)` | image PNG |
| `ui_dump(device_id)` | XML `uiautomator dump` (pour trouver les éléments) |
| `ui_tap(device_id, x, y)` / `ui_text(device_id, text)` / `ui_key(device_id, key)` | interaction écran |
| `run_instrumented_tests(device_id, test_package, runner=…, test_class=None)` | `am instrument -w -r`, résultat structuré |
| `shell(device_id, command)` | commande shell limitée à une liste blanche |

## 6. Sécurité

- **Bind** : `127.0.0.1` par défaut. Écoute sur une autre adresse (LAN) **refusée au
  démarrage** si aucun token n'est configuré.
- **Auth HTTP** : `Authorization: Bearer <token>` comparé en temps constant ; token lu
  depuis la variable d'environnement nommée dans la config (`IA_GATE_TOKEN` par défaut).
  Protection DNS-rebinding du SDK activée avec `allowed_hosts` = hôte(s) configuré(s).
- **APK** : `apk_path` doit se résoudre (chemin réel, liens résolus) sous un des
  `policy.apk_dirs`, se terminer par `.apk` et exister.
- **Shell** : la commande est découpée (`shlex`) ; refusée si elle contient un
  métacaractère (`; & | \` $ > < ( )` ou retour ligne) ; ses premiers mots doivent
  correspondre à une entrée de `policy.shell_allowlist`. Tous les arguments envoyés à
  `adb shell` sont échappés (`shlex.quote`).
- **Audit** : chaque appel de tool est journalisé dans `~/.ia-gate/audit.jsonl`
  (horodatage, tool, arguments, succès, durée, erreur).

## 7. Configuration (`~/.ia-gate/config.toml`, tout est optionnel)

```toml
[server]
host = "127.0.0.1"
port = 8765
token_env = "IA_GATE_TOKEN"

[adb]
path = "adb"
timeout_s = 120

[storage]
registry = "~/.ia-gate/devices.json"
audit_log = "~/.ia-gate/audit.jsonl"

[policy]
apk_dirs = ["~/ia-gate-apks"]
shell_allowlist = ["getprop", "pm list", "pm path", "dumpsys", "settings get", "ls", "ps", "df", "wm size", "ip addr"]
max_log_bytes = 200000
```

## 8. Gestion des erreurs

Les erreurs sont renvoyées comme erreurs de tool MCP (`ToolError`) avec un message
**actionnable** :

| Situation | Message (sens) |
|---|---|
| ID inconnu | « Équipement VAL-099 non enrôlé. Équipements connus : … » |
| Enrôlé mais non connecté | « VAL-042 n'est pas connecté (dernier serial adb connu : …). Branchez-le ou `connect_wifi`. » |
| `unauthorized` | « Acceptez la clé RSA de débogage sur l'écran de VAL-042. » |
| `offline` | « VAL-042 est offline : débranchez/rebranchez ou `adb reconnect`. » |
| `adb` introuvable | « Binaire adb introuvable (`adb.path` = …). » |
| Timeout | « Commande adb expirée après N s sur VAL-042. » |
| Échec install | code Android (`INSTALL_FAILED_UPDATE_INCOMPATIBLE`…) + conseil (ex. désinstaller d'abord) |
| Conflit d'enrôlement | ID déjà pris, ou appareil déjà enrôlé sous un autre ID |

## 9. Intégration des clients

- **Claude Code** : `.mcp.json` à la racine des projets consommateurs :
  `{"mcpServers":{"ia-gate":{"type":"http","url":"http://localhost:8765/mcp","headers":{"Authorization":"Bearer ${IA_GATE_TOKEN}"}}}}`
- **Claude Desktop** : stdio (`ia-gate serve --transport stdio`) ou pont
  `npx mcp-remote http://<pc>:8765/mcp --header "Authorization: Bearer …"`.
- **claude.ai (phase 2)** : tunnel sortant (Cloudflare Tunnel) + OAuth (`token_verifier` /
  `AuthSettings` du SDK) ; aucun port entrant.

## 10. Agents de développement et orchestrateurs (versionnés dans le dépôt)

| Fichier | Rôle |
|---|---|
| `.claude/agents/gate-implementer.md` | implémente **une** tâche du plan en TDD, commit |
| `.claude/agents/spec-reviewer.md` | vérifie la conformité d'une tâche à la spec et au plan |
| `.claude/agents/gate-code-reviewer.md` | revue qualité / sécurité / tests d'un diff |
| `.claude/agents/device-tester.md` | agent d'exécution : installe et teste un APK sur un équipement via les tools `ia-gate` |
| `.claude/workflows/implement-plan.js` | orchestrateur : pour chaque tâche → implémenter → 2 revues en parallèle → corrections (≤ 3 tours) → revue finale de branche |
| `.claude/workflows/device-test.js` | orchestrateur : teste un APK sur N équipements en parallèle (un `device-tester` par équipement), rapport consolidé |

Les sous-agents Claude Code ne peuvent pas lancer d'autres sous-agents : l'orchestration est
donc portée par des **workflows** (`.claude/workflows/`) lancés depuis la session principale.

## 11. Tests

- **Unitaires** (CI, sans équipement) : un `FakeRunner` remplace `subprocess` et rejoue des
  sorties adb réelles (`adb devices -l`, `pm install`, `am instrument`…).
- **Intégration MCP** : client MCP en mémoire (`mcp.client.Client(server)`) contre le
  serveur construit avec le `FakeRunner`.
- **E2E manuel** : checklist sur un vrai équipement (`docs/e2e-checklist.md`).

## 12. Phases suivantes

1. **v1** (ce document) : gate locale, Claude Code + Desktop.
2. **v2** : claude.ai via tunnel sortant + OAuth ; upload d'APK par URL d'artefact Jenkins.
3. **v3** : app compagnon Kotlin (tools matériels), installée par la gate et jointe via `adb forward`.
