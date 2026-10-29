# IA Gate — checklist E2E (équipement réel)

À dérouler avant chaque livraison, sur le PC de la gate, avec au moins **deux** équipements réels
(le second sert au test parallèle). Installation et configuration des clients :
[client-setup.md](client-setup.md).

## Préparation

- [ ] `uv tool install --reinstall ./gate` puis `ia-gate --help` liste `serve`, `devices`, `enroll`, `token`.
- [ ] `export IA_GATE_TOKEN="$(ia-gate token)"` puis `ia-gate serve --transport http` démarre sur `http://127.0.0.1:8765/mcp`.
- [ ] Claude Code connecté (`.mcp.json` copié de `.mcp.json.example`, ou `claude mcp add …`) : `claude mcp list` montre `ia-gate` connecté.
- [ ] Une app exemple et son APK de test (`app-debug.apk`, `app-debug-androidTest.apk`) sont copiés dans `~/ia-gate-apks/`.
- [ ] Aucun enregistrement `TEST-001` ni `TEST-002` dans `~/.ia-gate/devices.json`.

## Détection et enrôlement

- [ ] **Détection USB** : brancher l'équipement (débogage USB actif, clé RSA acceptée) → `ia-gate devices` et `list_devices` le montrent `online`, non enrôlé, avec son serial adb.
- [ ] **Enrôlement** : `enroll_device(adb_serial=<serial adb>, device_id="test-001")` → répond `TEST-001` (ID mis en majuscules) ; `adb -s <serial adb> shell cat /data/local/tmp/.ia-gate-id` affiche `TEST-001`.
- [ ] Ré-enrôler le même appareil sous `TEST-009` est refusé (appareil déjà enrôlé sous un autre ID), avec un message en français.
- [ ] **Redémarrage** : `adb -s <serial adb> reboot`, attendre la fin du boot → `list_devices` montre toujours `TEST-001` `online`.
- [ ] **Passage en Wi-Fi** : `adb -s <serial adb> tcpip 5555`, débrancher le câble, `connect_wifi(host="<IP de l'équipement>")` → `list_devices` montre toujours `TEST-001`, avec le serial adb `<IP>:5555`.
- [ ] Rebrancher le câble (USB + Wi-Fi à la fois) → un seul `TEST-001`, `device_info(device_id="TEST-001")` fonctionne.
- [ ] Débrancher et `adb disconnect` → `device_info("TEST-001")` répond « TEST-001 n'est pas connecté (dernier serial adb connu : …) », puis rebrancher.

## App : installation, lancement, observation

- [ ] `clear_logs("TEST-001")` réussit.
- [ ] `install_apk(device_id="TEST-001", apk_path="~/ia-gate-apks/app-debug.apk")` réussit.
- [ ] `install_apk` avec un chemin hors `apk_dirs` (ex. `/tmp/x.apk`, ou `~/ia-gate-apks/../x.apk`) est refusé sans rien installer.
- [ ] `launch_app(device_id="TEST-001", package="<package>")` renvoie un PID ; l'app est visible à l'écran.
- [ ] `screenshot("TEST-001")` renvoie une image PNG conforme à l'écran.
- [ ] `get_logs(device_id="TEST-001", package="<package>")` ne contient que les lignes de l'app ; `get_logs` sans `package` sur un logcat volumineux se termine par les lignes les plus récentes avec un avis de troncature.
- [ ] `ui_dump("TEST-001")` renvoie le XML de l'écran ; `ui_tap` sur un bouton trouvé dans le dump déclenche l'action ; `ui_text` / `ui_key(key="KEYCODE_BACK")` agissent.
- [ ] `shell(device_id="TEST-001", command="getprop ro.build.version.release")` répond ; `shell(..., command="rm -rf /sdcard")` et `shell(..., command="ls; reboot")` sont refusés.
- [ ] `stop_app` puis `uninstall_app(device_id="TEST-001", package="<package>")` réussissent.

## Tests instrumentés

- [ ] Installer l'app exemple et son APK de test avec `install_apk`.
- [ ] `run_instrumented_tests(device_id="TEST-001", test_package="<package>.test")` → nombres `passed` / `failed` exacts, `completed = true`.
- [ ] Avec un test volontairement en échec : l'échec apparaît avec `Classe#méthode` et le début de la pile.
- [ ] Avec `test_class="<Classe>#<méthode>"` : seul ce test est exécuté.

## Erreurs actionnables

- [ ] **`unauthorized`** : sur l'équipement, Options développeur → « Révoquer les autorisations de débogage USB », rebrancher sans accepter → `device_info("TEST-001")` répond « Acceptez la clé RSA de débogage sur l'écran de TEST-001 … » ; accepter → l'appel suivant réussit.
- [ ] `device_info("TEST-099")` → « Équipement TEST-099 non enrôlé. Équipements connus : … ».
- [ ] Gate lancée avec `adb.path = "/nulle/part/adb"` → les tools répondent « Binaire adb introuvable (`adb.path` = …) ».
- [ ] Requête HTTP sans token (`curl -i -X POST http://127.0.0.1:8765/mcp`) → 401 ; avec `host = "0.0.0.0"` et sans `IA_GATE_TOKEN`, `ia-gate serve --transport http` refuse de démarrer.

## Parallélisme

- [ ] Enrôler un second équipement `TEST-002`.
- [ ] Lancer le workflow `device-test` (ou deux sessions Claude Code) pour que **deux agents** installent en même temps le même APK, l'un sur `TEST-001`, l'autre sur `TEST-002` → les deux installations réussissent, chacune sur son équipement ; dans `~/.ia-gate/audit.jsonl` les deux `install_apk` se chevauchent dans le temps.
- [ ] Deux appels simultanés sur **le même** équipement (ex. `install_apk` et `screenshot` sur `TEST-001`) s'exécutent l'un après l'autre sans erreur.

## Nettoyage

- [ ] Retirer `TEST-001` et `TEST-002` de `~/.ia-gate/devices.json` et supprimer `/data/local/tmp/.ia-gate-id` sur les équipements si besoin.
