# IA Gate — installation et configuration des clients

Ce guide installe la gate sur le PC relié aux équipements Android, la lance comme service, puis
branche Claude Code et Claude Desktop dessus. Référence : [spécification](superpowers/specs/2026-10-29-ia-gate-design.md)
(§6 sécurité, §7 configuration, §9 clients).

## 1. Installation

Prérequis sur le PC de la gate :

- Python ≥ 3.11 et [`uv`](https://docs.astral.sh/uv/) ;
- `adb` (Android platform-tools) dans le `PATH`, ou son chemin dans `adb.path` (voir plus bas).

Depuis la racine du dépôt :

```bash
uv tool install ./gate      # installe la commande `ia-gate` (dans ~/.local/bin)
ia-gate --help              # sous-commandes : serve, devices, enroll, token
adb devices -l              # vérifie qu'adb voit les équipements
```

Mise à jour après un `git pull` : `uv tool install --reinstall ./gate`.

### Configuration (optionnelle)

Tout est optionnel : sans fichier, les valeurs par défaut ci-dessous s'appliquent. Le fichier est
`~/.ia-gate/config.toml` (ou un autre chemin via `--config PATH`, accepté par `serve`, `devices`
et `enroll`).

```toml
[server]
host = "127.0.0.1"
port = 8765
token_env = "IA_GATE_TOKEN"
# allowed_hosts = ["192.168.1.10", "gatepc.local"]  # noms/IP acceptés dans l'en-tête Host

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

Les APK à installer doivent se trouver sous un des dossiers `policy.apk_dirs` (par défaut
`~/ia-gate-apks`, à créer) : `install_apk` refuse tout autre chemin.

### Token

```bash
ia-gate token                         # affiche un token aléatoire
export IA_GATE_TOKEN='<token affiché>'
```

Le token est lu dans la variable d'environnement nommée par `server.token_env`. Il est
**obligatoire** dès que la gate écoute ailleurs que sur `127.0.0.1` / `localhost` / `::1` :
sinon `ia-gate serve --transport http` refuse de démarrer (code de sortie 2). Les clients doivent
connaître la même valeur.

### Accès depuis le réseau privé (LAN)

Pour que d'autres postes joignent la gate :

```toml
[server]
host = "0.0.0.0"
allowed_hosts = ["192.168.1.10", "gatepc.local"]  # IP LAN et/ou nom de ce PC, sans port
```

`allowed_hosts` alimente la protection DNS-rebinding : un client qui utilise un nom ou une IP
absent de la liste reçoit un HTTP 421 indiquant quoi ajouter. Avec `host = "0.0.0.0"` et sans
`allowed_hosts`, la gate affiche un avertissement au démarrage.

## 2. Lancer la gate comme service

En mode HTTP, un seul processus est partagé par tous les clients (recommandé).

```bash
IA_GATE_TOKEN='<token>' ia-gate serve --transport http   # http://<hôte>:8765/mcp
```

### Linux : unité systemd utilisateur

`~/.config/systemd/user/ia-gate.service` :

```ini
[Unit]
Description=IA Gate (serveur MCP adb)
After=network-online.target

[Service]
# Fichier contenant la ligne IA_GATE_TOKEN=<token> (chmod 600)
EnvironmentFile=%h/.ia-gate/env
# systemd n'hérite pas du PATH du shell : adb doit y être, ou renseignez adb.path dans config.toml
Environment=PATH=%h/.local/bin:%h/Android/Sdk/platform-tools:/usr/local/bin:/usr/bin:/bin
ExecStart=%h/.local/bin/ia-gate serve --transport http
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

```bash
mkdir -p ~/.ia-gate && printf 'IA_GATE_TOKEN=%s\n' "$(ia-gate token)" > ~/.ia-gate/env && chmod 600 ~/.ia-gate/env
systemctl --user daemon-reload
systemctl --user enable --now ia-gate
loginctl enable-linger "$USER"        # démarre le service sans session ouverte
journalctl --user -u ia-gate -f       # journaux
```

### Windows : Planificateur de tâches

Dans PowerShell (utilisateur qui a installé la gate) :

```powershell
# Token persistant dans l'environnement utilisateur (rouvrir les sessions ensuite)
$token = ia-gate token
[Environment]::SetEnvironmentVariable("IA_GATE_TOKEN", $token, "User")

$exe = "$env:USERPROFILE\.local\bin\ia-gate.exe"
$action = New-ScheduledTaskAction -Execute $exe -Argument "serve --transport http"
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit 0
Register-ScheduledTask -TaskName "IA Gate" -Action $action -Trigger $trigger -Settings $settings
Start-ScheduledTask -TaskName "IA Gate"
```

Équivalent en une ligne : `schtasks /Create /TN "IA Gate" /SC ONLOGON /TR "\"%USERPROFILE%\.local\bin\ia-gate.exe\" serve --transport http"`.
Vérifiez que `adb.exe` est dans le `PATH` utilisateur (ou renseignez `adb.path`), et autorisez le
port 8765 dans le pare-feu Windows uniquement pour le réseau privé si la gate est jointe en LAN.

## 3. Claude Code

Claude Code parle à la gate en HTTP. La variable `IA_GATE_TOKEN` doit être définie dans le
shell qui lance `claude`.

**Option A — `.mcp.json` à la racine du projet consommateur** (partageable, le token n'est pas
écrit dans le fichier) : copiez [`.mcp.json.example`](../.mcp.json.example) :

```json
{
  "mcpServers": {
    "ia-gate": {
      "type": "http",
      "url": "http://localhost:8765/mcp",
      "headers": {
        "Authorization": "Bearer ${IA_GATE_TOKEN}"
      }
    }
  }
}
```

Remplacez `localhost` par l'IP ou le nom du PC de la gate si elle tourne sur une autre machine
(ce nom doit figurer dans `server.allowed_hosts`).

**Option B — `claude mcp add`** (la valeur du token est enregistrée dans la configuration
locale de Claude Code) :

```bash
claude mcp add --transport http ia-gate http://localhost:8765/mcp \
  --header "Authorization: Bearer $IA_GATE_TOKEN"
claude mcp list                        # ia-gate doit apparaître connecté
```

Dans une session Claude Code, `/mcp` liste les tools `ia-gate` (`list_devices`, `install_apk`…).

## 4. Claude Desktop

Fichier `claude_desktop_config.json` (Paramètres → Développeur → Modifier la configuration ;
macOS `~/Library/Application Support/Claude/`, Windows `%APPDATA%\Claude\`). Redémarrez Claude
Desktop après modification.

**Stdio** — Claude Desktop lance lui-même la gate ; elle doit donc tourner **sur le même PC**
que les équipements. Pas de token ni de port. Claude Desktop n'hérite pas toujours du `PATH` :
indiquez le chemin absolu de `ia-gate` (`which ia-gate` / `where ia-gate`).

```json
{
  "mcpServers": {
    "ia-gate": {
      "command": "/home/alice/.local/bin/ia-gate",
      "args": ["serve", "--transport", "stdio"]
    }
  }
}
```

Évitez de piloter les mêmes équipements à la fois par ce processus stdio et par une gate HTTP :
les verrous par équipement ne sont pas partagés entre processus.

**Pont `mcp-remote`** — pour joindre une gate HTTP (locale ou sur un autre PC du LAN). Nécessite
Node.js (`npx`). `--allow-http` est requis par `mcp-remote` pour une URL `http://` autre que
`localhost` ; le token passe par une variable d'environnement (évite les problèmes d'espaces
dans les arguments sous Windows).

```json
{
  "mcpServers": {
    "ia-gate": {
      "command": "npx",
      "args": [
        "mcp-remote",
        "http://gatepc.local:8765/mcp",
        "--allow-http",
        "--header",
        "Authorization:${IA_GATE_AUTH}"
      ],
      "env": {
        "IA_GATE_AUTH": "Bearer <token>"
      }
    }
  }
}
```

## 5. Enrôler un équipement

Un équipement est désigné par un **ID gate** stable (`^[A-Z0-9][A-Z0-9_-]{1,31}$`, mis en
majuscules, ex. `VAL-042`), jamais par son serial adb.

1. Sur l'équipement : activer les options développeur et le **débogage USB**, brancher le câble,
   accepter la clé RSA à l'écran (cocher « Toujours autoriser »).
2. Repérer l'appareil non enrôlé et son serial adb :

   ```bash
   ia-gate devices
   # DEVICE_ID  STATE   ADB_SERIAL   MODEL   LABEL
   # -          online  R58M1234ABC  TPS900
   ```

   (ou, depuis Claude : « liste les équipements » → `list_devices`).
3. Enrôler :

   ```bash
   ia-gate enroll R58M1234ABC VAL-042 --label "Valideur quai 3"
   ```

   (ou `enroll_device(adb_serial="R58M1234ABC", device_id="VAL-042", label="Valideur quai 3")`).
   La gate enregistre l'empreinte (`ro.serialno`, `android_id`, fabricant, modèle) dans
   `~/.ia-gate/devices.json` et écrit l'ID dans `/data/local/tmp/.ia-gate-id` sur l'équipement.
4. Passage en Wi-Fi (optionnel), câble encore branché :

   ```bash
   adb -s R58M1234ABC tcpip 5555
   ```

   débrancher, puis `connect_wifi(host="192.168.1.50")` depuis Claude (ou
   `adb connect 192.168.1.50:5555`). `ia-gate devices` montre toujours `VAL-042`, avec le serial
   adb `192.168.1.50:5555`.
5. Vérifier : `ia-gate devices` affiche `VAL-042  online`. L'ID survit aux redémarrages, aux
   changements d'IP et au passage USB ↔ Wi-Fi ; un reset usine efface le marqueur et change
   l'`android_id` : l'appareil réapparaît alors non enrôlé (retirez son ancien ID de
   `devices.json` avant de le ré-enrôler sous le même ID).

## 6. Dépannage

Messages renvoyés par les tools (spec §8) :

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

Côté connexion HTTP :

| Symptôme | Cause / correction |
|---|---|
| `ia-gate serve` sort avec « Écoute sur … refusée : aucun token configuré » (code 2) | `server.host` n'est pas une adresse de bouclage : définissez `IA_GATE_TOKEN` (`ia-gate token`) ou revenez à `host = "127.0.0.1"`. |
| HTTP 401 `unauthorized` | En-tête `Authorization: Bearer <token>` absent ou différent du token de la gate : vérifiez `IA_GATE_TOKEN` côté client et côté gate. |
| HTTP 421 `invalid_host` | Le nom/IP utilisé dans l'URL n'est pas autorisé : ajoutez-le à `server.allowed_hosts` puis redémarrez la gate. |
| `install_apk` refuse le chemin | L'APK doit être sous un dossier `policy.apk_dirs`, finir par `.apk` et exister (liens symboliques résolus). |
| `shell` refuse la commande | Pas de métacaractère (`; & \| $ > < ( )` …) ; les premiers mots doivent figurer dans `policy.shell_allowlist`. |

Chaque appel de tool est journalisé dans `~/.ia-gate/audit.jsonl` (outil, arguments, succès,
durée, erreur).
