# 09. Host Nginx

## Устройство

Reverse proxy завершает публичное соединение и передаёт его upstream. Virtual host (`server` block) выбирается по `server_name`. `proxy_pass` указывает backend. Forwarded headers сохраняют исходные host, IP и protocol; request ID связывает логи. Timeouts ограничивают зависшие соединения; security headers уменьшают browser-риск.

На Ubuntu конфигурация обычно хранится в `/etc/nginx/sites-available` и включается symlink в `sites-enabled`. Это host Nginx, отдельный от контейнерного `gateway`. Reload перечитывает проверенную конфигурацию без обычного разрыва соединений; restart полностью перезапускает service.

## Пример планового server block

Ниже адаптация repository gateway template. Certbot позднее добавит TLS paths/redirect; до этого нужен HTTP virtual host для ACME.

```nginx
server {
    listen 80;
    listen [::]:80;
    server_name staging.fetchnow.online;

    location / {
        proxy_pass http://127.0.0.1:8091;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Request-ID $request_id;
        proxy_connect_timeout 5s;
        proxy_send_timeout 60s;
        proxy_read_timeout 60s;
    }
}
```

Host Nginx генерирует request ID; container gateway передаёт существующий `X-Request-ID`. Заголовки безопасности уже добавляет gateway, но финальную политику проверяют на публичном ответе.

## Команды и gate

```bash
sudo nginx -t
sudo systemctl reload nginx
sudo systemctl status nginx --no-pager
sudo journalctl -u nginx --since "10 minutes ago"
```

`nginx -t` обязателен перед reload: синтаксическая ошибка не должна заменить рабочую конфигурацию. Только после `test is successful` выполняется **WARNING modifying** reload; затем status, journal, FetchNow smoke и проверки CryptoBot/JustTwo.

## Ошибки и rollback

- `duplicate server_name`: найти все definitions `sudo nginx -T`.
- `502`: сначала `curl 127.0.0.1:8091`, затем gateway/api logs.
- `504`: upstream завис либо timeout слишком мал; не увеличивать его до диагностики.

**ROLLBACK:** сохранить предыдущий site file/symlink, вернуть его, `nginx -t`, reload и smoke всех virtual hosts. **DANGER:** не редактировать Certbot private keys, не reload при failed test и не заменять общую конфигурацию целиком.

