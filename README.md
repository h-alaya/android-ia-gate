# android-ia-gate

Passerelle **MCP** entre Claude (Claude Code, Claude Desktop) et des équipements Android métier.
Elle tourne sur un PC du réseau privé et pilote les équipements par **adb**. Chaque équipement
est désigné par un **identifiant unique stable** (ex. `VAL-042`) :

> « Installe `app-debug.apk` sur VAL-042 et lance les tests instrumentés. »

```
Claude Code / Desktop ──MCP (HTTP ou stdio)──▶ IA Gate (PC) ──adb USB/Wi-Fi──▶ équipements Android
```

## Statut

Conception en cours de validation : voir la [spécification](docs/superpowers/specs/2026-10-29-ia-gate-design.md)
et le [plan d'implémentation](docs/superpowers/plans/2026-10-29-ia-gate-v1.md).

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
