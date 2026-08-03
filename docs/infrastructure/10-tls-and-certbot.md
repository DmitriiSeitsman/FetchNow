# 10. TLS и Certbot

## Как устроено

TLS шифрует соединение и подтверждает имя сервера. Certificate связывает public key и hostname; private key остаётся секретным. CA (certificate authority) подписывает certificate. Let's Encrypt — публичная CA, Certbot — ACME-клиент. ACME challenge доказывает контроль домена. SNI передаёт hostname во время TLS handshake, позволяя нескольким сайтам делить `:443`. Certificate chain связывает leaf certificate с доверенным root.

DNS должен указывать на сервер до выпуска: HTTP-01 challenge придёт по имени на port 80. Nginx virtual host должен проходить config test и быть публично доступен.

## Preflight и выпуск

```bash
dig staging.fetchnow.online A +short
sudo nginx -t
curl -I http://staging.fetchnow.online/
sudo certbot certificates
systemctl status certbot.timer
sudo certbot renew --dry-run
```

Первые три — **CHECK**: ожидаются `77.239.107.143`, successful Nginx test и ответ именно staging vhost. `certbot certificates` показывает names/expiry без private key; timer должен быть active; dry-run безопасно проверяет renewal, но обращается к staging CA.

**CHECK/WARNING — реальный выпуск меняет Nginx и certificate storage:**

```bash
sudo certbot --nginx -d staging.fetchnow.online
```

Выполнять только после DNS/Nginx preflight, backup site config и проверки соседей. Выбрать HTTPS redirect, затем повторить `nginx -t`, HTTPS smoke и neighbour checks.

## Проверка

```bash
curl -vI https://staging.fetchnow.online/
openssl s_client -connect staging.fetchnow.online:443 \
  -servername staging.fetchnow.online </dev/null
```

Проверяются hostname, chain, expiry и HTTP response. Не использовать `curl -k`: он отключает validation и скрывает проблему.

## Ошибки и rollback

- Challenge 404: неверный vhost/routing.
- Timeout: DNS/firewall/port 80.
- Name mismatch: запрос попал в другой server block или certificate.

**ROLLBACK:** вернуть сохранённый Nginx site, `nginx -t`, reload. Не удалять certificates в панике: сначала выяснить, используются ли они другими vhosts. Никогда не показывать содержимое `/etc/letsencrypt/live/*/privkey.pem`.

