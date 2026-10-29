# android-ia-gate

Passerelle **MCP** entre Claude (Claude Code, Claude Desktop) et des équipements Android métier.
Elle tourne sur un PC du réseau privé et pilote les équipements par **adb**. Chaque équipement
est désigné par un **identifiant unique stable** (ex. `VAL-042`) :

> « Installe `app-debug.apk` sur VAL-042 et lance les tests instrumentés. »

```
Claude Code / Desktop ──MCP (HTTP ou stdio)──▶ IA Gate (PC) ──adb USB/Wi-Fi──▶ équipements Android
```

## Statut

v1 : voir la [spécification](docs/superpowers/specs/2026-10-29-ia-gate-design.md)
et le [plan d'implémentation](docs/superpowers/plans/2026-10-29-ia-gate-v1.md).

## Installation

Prérequis sur le PC de la gate : Python ≥ 3.11, [`uv`](https://docs.astral.sh/uv/) et `adb`
(Android platform-tools) dans le `PATH`.

```bash
uv tool install ./gate     # installe la commande `ia-gate`
ia-gate --help             # sous-commandes : serve, devices, enroll, token
```

Configuration optionnelle dans `~/.ia-gate/config.toml` ; service systemd / Planificateur de
tâches Windows, accès LAN et Claude Desktop : voir [docs/client-setup.md](docs/client-setup.md).

## Démarrage rapide

```bash
mkdir -p ~/ia-gate-apks                          # seuls les APK de ce dossier peuvent être installés
export IA_GATE_TOKEN="$(ia-gate token)"
ia-gate serve --transport http                   # http://127.0.0.1:8765/mcp

# dans un autre terminal : enrôler un équipement branché en USB
ia-gate devices                                  # repérer son ADB_SERIAL
ia-gate enroll R58M1234ABC VAL-042 --label "Valideur quai 3"
```

Côté Claude Code, copiez [`.mcp.json.example`](.mcp.json.example) en `.mcp.json` à la racine du
projet (ou `claude mcp add --transport http ia-gate http://localhost:8765/mcp --header "Authorization: Bearer $IA_GATE_TOKEN"`),
avec la même valeur d'`IA_GATE_TOKEN` dans le shell qui lance `claude`, puis demandez par exemple : « Installe `~/ia-gate-apks/app-debug.apk` sur VAL-042 et lance l'app. »

Validation sur équipement réel : [docs/e2e-checklist.md](docs/e2e-checklist.md).

## Développement assisté par agents

| Élément | Rôle |
|---|---|
| `.claude/agents/gate-implementer.md` | implémente une tâche du plan en TDD |
| `.claude/agents/spec-reviewer.md` | vérifie la conformité à la spec / au plan |
| `.claude/agents/gate-code-reviewer.md` | revue qualité et sécurité |
| `.claude/agents/device-tester.md` | installe et teste un APK sur un équipement via la gate |
| `.claude/workflows/implement-plan.js` | orchestre : implémenter → revues parallèles → corrections → revue finale |
| `.claude/workflows/device-test.js` | orchestre un test sur plusieurs équipements en parallèle |

Exemple, depuis Claude Code à la racine du dépôt :

> « Lance le workflow `implement-plan` avec `{ plan: "docs/superpowers/plans/2026-10-29-ia-gate-v1.md", tasks: [1, 2, 3] }` »
