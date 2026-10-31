# Откат

Подставьте каталог резервной копии, рабочую папку и имя службы из вывода установщика. Для стандартной установки:

```bash
sudo systemctl stop awg_bot
sudo tar -xzf /var/backups/amnezia-manager/ДАТА-PID/installation.tar.gz -C /opt/amnezia-manager
sudo cp /var/backups/amnezia-manager/ДАТА-PID/previous.service /etc/systemd/system/awg_bot.service
sudo systemctl daemon-reload
sudo systemctl start awg_bot
```

previous.service существует только при обновлении ранее установленной службы. Если менялся режим интерфейса, восстановите сохранённый interface.conf в /etc/systemd/system/awg_bot.service.d/zz-interface.conf; если файла до обновления не было, удалите созданный zz-interface.conf. Другие drop-in не изменяйте.

Старые Python-окружения сохранены; восстановленный unit указывает на прежнее. При откате до исходного бота неиспользуемые новые файлы могут остаться в каталоге, старая точка входа их не импортирует.

Откат БД возвращает её к моменту копии и может убрать статистику/действия после обновления. Откат кода не отменяет изменения peer, уже применённые на VPN-серверах. Не останавливайте VPN-контейнеры для отката бота.

Настройка HTTPS сохраняет отдельную копию Caddyfile в /var/backups/amnezia-manager/https-*. Восстанавливать её нужно только если после этого не менялись другие сайты. Никогда не заменяйте общий Caddyfile вслепую.
