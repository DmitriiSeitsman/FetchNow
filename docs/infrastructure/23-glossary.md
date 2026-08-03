# 23. Глоссарий

| Термин | Краткое объяснение |
|---|---|
| A record | DNS-запись имени на IPv4-адрес. |
| AAAA record | DNS-запись имени на IPv6-адрес. |
| ACME | Протокол автоматического выпуска TLS-сертификатов. |
| Authoritative DNS | Сервер-источник истины для DNS-зоны. |
| Backup | Отдельная копия данных, предназначенная для восстановления. |
| Bind | Привязка socket к локальному адресу и порту. |
| Bind mount | Подключение конкретного host path в container. |
| Bounded GET | Streaming GET, который читает не больше заданного диагностического лимита. |
| CA | Центр сертификации, подписывающий certificates. |
| CDN | Распределённая сеть доставки контента. |
| Certificate | Подписанная связь имени и public key. |
| CNAME | DNS alias на другое доменное имя. |
| Compose project | Изолированная группа Compose resources с общим именем. |
| Concurrency | Число одновременно выполняемых работ. |
| Content type | Тип содержимого HTTP response, обычно выраженный MIME. |
| Container | Запущенный изолированный экземпляр image. |
| Daemon | Долговременно работающий фоновый процесс. |
| Database | Именованная коллекция данных внутри DB server. |
| DNS | Система преобразования доменных имён в записи. |
| DNS rebinding | Смена DNS-ответа во времени для обхода проверки destination. |
| DNS TOCTOU | Race между проверкой DNS и фактическим сетевым соединением. |
| Docker daemon | Сервис, управляющий Docker resources. |
| Environment variable | Пара `имя=значение`, переданная процессу как configuration. |
| Exit code | Число результата процесса; обычно 0 означает успех. |
| Firewall | Фильтр сетевого трафика по правилам. |
| Gateway | Входной компонент, маршрутизирующий запросы к сервисам. |
| Graceful shutdown | Завершение с прекращением новых работ и корректной очисткой. |
| Healthcheck | Автоматическая проверка состояния компонента. |
| Idempotency | Повтор операции не меняет итог после первого успешного применения. |
| Image | Неизменяемый шаблон filesystem/config для containers. |
| IDNA | Правила преобразования Unicode-домена в сравнимое ASCII-представление. |
| Immutable image | Image, который заменяют новой версией, а не правят на сервере. |
| IP pinning | Соединение именно с предварительно проверенным IP при сохранении hostname/TLS semantics. В PR2 не реализовано. |
| Inode | Запись filesystem о файле; отдельный ограниченный ресурс. |
| Journal | Хранилище событий systemd. |
| Layer | Неизменяемый слой Docker image. |
| Least privilege | Выдача только минимально необходимых прав. |
| Liveness | Проверка, что процесс жив и способен ответить. |
| Log rotation | Ограничение/архивация старых logs. |
| Loopback | Сеть host к самому себе (`127.0.0.1`, `::1`). |
| Migration | Версионированное изменение database schema/data. |
| MIME | Стандартное имя типа данных, например `text/html`. |
| MX | DNS-запись маршрута электронной почты. |
| NAT | Преобразование сетевых адресов/портов. |
| Network | Канал связи; в Compose также изолированная виртуальная сеть. |
| Non-root | Процесс без UID 0 и полных системных прав. |
| PID | Числовой идентификатор процесса. |
| Persistent state | Данные, которые должны пережить restart/recreate. |
| Port | Номер транспортного endpoint TCP/UDP. |
| Process | Экземпляр выполняющейся программы. |
| Provider allowlist | Закрытый список точных hostnames разрешённого provider. |
| Queue | Очередь работ для последующей обработки. |
| Rate limit | Ограничение частоты запросов/операций. |
| Readiness | Готовность обслуживать traffic с обязательными dependencies. |
| Recursive resolver | DNS-сервис, находящий и кеширующий ответ для клиента. |
| Registry | Хранилище и distribution service для images. |
| Release | Конкретная развёртываемая версия кода/images/config metadata. |
| Request ID | Идентификатор для корреляции одного запроса между logs. |
| Redirect hop | Один переход от текущего URL к значению `Location`, проверяемый заново. |
| Restore | Восстановление данных из backup. |
| Reverse proxy | Сервер, принимающий запрос и пересылающий upstream. |
| Rollback | Возврат к прежней проверенной версии/состоянию. |
| Secret | Чувствительное значение: password, token или private key. |
| Service | Управляемая функция системы или Compose-компонент. |
| SIGTERM | Signal с просьбой корректно завершиться. |
| Signal | Асинхронное уведомление Unix-процессу. |
| SNI | Передача hostname в TLS handshake для выбора certificate. |
| Snapshot | Снимок storage/VM на конкретный момент. |
| Socket | Endpoint взаимодействия, часто `IP:port + protocol`. |
| SSRF | Принуждение сервера делать запросы к запрещённым адресам. |
| Stateless service | Сервис без незаменимого локального persistent state. |
| Stable error code | Машиночитаемый публичный код ошибки, не зависящий от внутренних exception details. |
| stdout/stderr | Стандартные потоки обычного и диагностического вывода. |
| Streaming response | Response body, читаемый частями без загрузки целиком в память. |
| Swap | Disk-backed область для вытесненных memory pages. |
| systemd | Менеджер services и boot lifecycle в Ubuntu. |
| TCP | Надёжный потоковый транспортный протокол. |
| TLS | Шифрование и аутентификация сетевого соединения. |
| TLS verification | Проверка certificate chain, срока и соответствия hostname. |
| Transaction | Атомарная группа database operations. |
| TTL | Время жизни DNS cache или объекта по policy. |
| Upstream | Внутренний сервис, которому proxy передаёт запрос. |
| URL normalization | Приведение scheme/hostname/port/path к однозначному безопасному виду. |
| Virtual host | Конфигурация веб-сервера для конкретного имени. |
| Volume | Docker-managed persistent storage. |
| VPS | Виртуальный сервер с собственной гостевой OS. |
| Worker | Фоновый процесс обработки очереди/задач. |
