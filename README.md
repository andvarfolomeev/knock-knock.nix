# knock-knock.nix

NixOS-модуль: Telegram-бот включает и выключает WireGuard-клиент по командам и сам
опускает туннель, если сервер перестал отвечать. Так машина не остаётся без
интернета, когда весь трафик завёрнут в туннель, а сервер упал.

Бот написан на Python, использует только стандартную библиотеку и запускается
через systemd.

## Подключение

```nix
{
  inputs.knock-knock.url = "github:<you>/knock-knock.nix";

  outputs = { nixpkgs, knock-knock, ... }: {
    nixosConfigurations.host = nixpkgs.lib.nixosSystem {
      modules = [
        knock-knock.nixosModules.default
        ./configuration.nix
      ];
    };
  };
}
```

```nix
{
  networking.wg-quick.interfaces.wg0 = {
    autostart = false;            # обязательно: туннель поднимает только бот
    address = [ "10.0.0.2/24" ];
    privateKeyFile = "/etc/wireguard/wg0.key";
    peers = [{
      publicKey = "...";
      endpoint = "vpn.example.com:51820";
      allowedIPs = [ "0.0.0.0/0" "::/0" ];
      persistentKeepalive = 25;
    }];
  };

  services.knock-knock = {
    enable = true;
    interface = "wg0";
    tokenFile = "/run/secrets/knock-knock-token";  # sops-nix, agenix или просто файл
    allowedChatIds = [ 123456789 ];
    checkHost = "10.0.0.1";      # адрес сервера внутри туннеля
    downTimeout = 60;
  };
}
```

Если интерфейс описан через `networking.wireguard.interfaces`, укажите
`unit = "wireguard-wg0.service"` и отключите его автозапуск сами.

## Команды

| Команда   | Что делает |
|-----------|------------|
| `/on`     | Поднимает туннель. Если задан `preCheckHost`, сначала пингует его. |
| `/off`    | Опускает туннель. |
| `/status` | Состояние и время последнего ответа сервера. |

Ваш chat id можно узнать у `@userinfobot`. Сообщения из чатов не из
`allowedChatIds` игнорируются.

## Как работает watchdog

- Пока юнит туннеля активен, бот каждые `checkInterval` секунд проверяет сервер:
  пингует `checkHost` через интерфейс, а без него смотрит возраст последнего
  handshake в `wg show` (реагирует медленнее, нужен `persistentKeepalive`).
- Если успешной проверки не было `downTimeout` секунд (отсчёт от включения или
  последнего ответа), бот останавливает юнит и пишет в Telegram. Сообщение уйдёт,
  как только вернётся обычный интернет.
- Watchdog следит за туннелем, даже если его подняли вручную или бот
  перезапустился.
- После перезагрузки туннель выключен (`autostart = false`, модуль это проверяет
  assertion'ом).

## Опции

| Опция             | По умолчанию                  | Описание |
|-------------------|-------------------------------|----------|
| `interface`       | —                             | Имя WireGuard-интерфейса. |
| `unit`            | `wg-quick-<interface>.service` | systemd-юнит туннеля. |
| `tokenFile`       | —                             | Файл с токеном бота, передаётся через `LoadCredential`. |
| `allowedChatIds`  | —                             | Кому разрешено управлять и кому слать уведомления. |
| `checkHost`       | `null`                        | Что пинговать через туннель. |
| `preCheckHost`    | `null`                        | Что пинговать перед включением. |
| `checkInterval`   | `5`                           | Период проверки, с. |
| `downTimeout`     | `60`                          | Сколько сервер может молчать, с. |
| `pingTimeout`     | `2`                           | Таймаут одного ping, с. |
| `handshakeMaxAge` | `180`                         | Порог возраста handshake, если `checkHost = null`. |
