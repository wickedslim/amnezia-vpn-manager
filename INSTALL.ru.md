# Установить с нуля

Устанавливается менеджер VPN, а не сам VPN. Требуется работающий AmneziaWG в Docker на одном или нескольких VPS, Ubuntu 24.04 / Debian 12 с systemd и доступ root/sudo. На каждом управляемом VPN-сервере SSH-пользователь должен иметь неинтерактивный доступ к Docker.

## Быстрая установка одной командой

```bash
bash -c 'set -e; run_root() { if [ "$EUID" -eq 0 ]; then "$@"; else sudo "$@"; fi; }; command -v git >/dev/null || { run_root apt-get update; run_root apt-get install -y git ca-certificates; }; task_dir=$(mktemp -d); trap "rm -rf -- \"$task_dir\"" EXIT; git -c credential.helper= clone --depth 1 https://github.com/wickedslim/amnezia-vpn-manager.git "$task_dir"; run_root bash "$task_dir/install.sh"'
```

Команда скачивает актуальную ветку `main` в новую временную папку, запускает установщик и удаляет только эту временную копию после завершения. Если Git отсутствует, установит Git и сертификаты через apt. Запускайте в терминале VPS (Ubuntu/Debian); если вы не root, потребуется sudo.

**Репозиторий приватный:** сначала примите приглашение. Git запросит GitHub-логин и `Password`: сюда вводится **личный access token GitHub**, а не пароль аккаунта и не токен Telegram-бота. Ввод скрытый; команда отключает сохранение токена через Git credential helper. Не вставляйте токен в URL или текст команды. Подробнее — [авторизация GitHub по HTTPS](https://docs.github.com/en/get-started/git-basics/about-remote-repositories#cloning-with-https-urls). Если на сервере уже настроен SSH-доступ к GitHub, замените HTTPS-адрес на `git@github.com:wickedslim/amnezia-vpn-manager.git`.

Установщик запросит настройки бота и спросит, нужен ли Mini App. Для установки только бота замените в конце `"$task_dir/install.sh"` на `"$task_dir/install.sh" --bot-only`.

## 1. Получить исходники вручную

Примите приглашение в приватный репозиторий. Откройте [главную](https://github.com/wickedslim/amnezia-vpn-manager) → **Code → Download ZIP**, распакуйте и перенесите на сервер. Или клонируйте по SSH, если GitHub-ключ уже настроен:

```bash
git clone git@github.com:wickedslim/amnezia-vpn-manager.git
cd amnezia-vpn-manager
```

Не передавайте другу токен уже работающего бота: каждый владелец использует свой токен и Telegram ID.

## 2. Выбрать интерфейс

### Только Telegram-бот

Не требует домена, Caddy или открытых веб-портов:

```bash
sudo bash install.sh --bot-only
```

### Бот + Mini App

Создайте DNS A для своего поддомена на IP сервера с ботом. Если есть AAAA, он тоже должен указывать на этот сервер. Откройте TCP 80/443 в firewall провайдера. Затем:

```bash
sudo bash install.sh --domain vpn.example.com --ip 203.0.113.10
```

Замените пример домена/IP на свои. Вариант `sudo bash install.sh` спросит, нужен ли Mini App; по умолчанию — нет. На сервере с уже существующим HTTPS-прокси автоматический установщик не заменит чужие сайты: настройте его вручную по [MINIAPP.ru.md](MINIAPP.ru.md).

## 3. Настроить бота

Установщик запросит токен BotFather (скрытый ввод) и Telegram ID владельца. Код устанавливается в `/opt/amnezia-manager`, служба — `awg_bot`, отдельное Python-окружение — в `.venvs/` рабочей папки.

Откройте бота → `/servers` → «Подключить сервер». Введите название, SSH-адрес, пользователя/порт, пароль или путь к ключу и адрес VPN для выдаваемых конфигов. Управление несколькими VPN выполняет одна копия бота; повторно запускать тот же токен на другом VPS не нужно.

Если включён Mini App, у владельца появится кнопка «Панель VPN». Доступ API проверяет подписанные Telegram initData, срок сессии и ID владельца.

## Ограничения

Старые приватные ключи, которых нет в доступных сохранённых файлах, восстановить невозможно. Месячная история начнётся с первого наблюдения; первый замер нового конфига служит исходной точкой. Speedtest устанавливается только после отдельного подтверждения в интерфейсе.
