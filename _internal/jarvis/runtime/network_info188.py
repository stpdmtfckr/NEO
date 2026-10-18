"""NEO 188 HIST-060: a dedicated, privacy-aware text for «сетевая информация».

The generic result projection deliberately shows only allow-listed scalar fields,
so the network details (computer name, interface addresses, link speed) never
reached the answer. This formatter shows exactly those useful fields:

* the computer name;
* for each interface that is UP: its name, IPv4 address(es) and speed;
* an explicit note when a part could not be read («частично»).

Never shown: MAC addresses, IPv6 link-local addresses, netmask/broadcast dumps,
or any raw structure. Loopback interfaces are skipped.
"""
from __future__ import annotations

import ipaddress
import socket
from typing import Any

_CODES = ('ru', 'en', 'de', 'fr', 'es', 'it', 'pt')
_T = {
    'title': ('Сеть этого компьютера', 'This computer’s network', 'Netzwerk dieses Computers', 'Réseau de cet ordinateur',
              'Red de este equipo', 'Rete di questo computer', 'Rede deste computador'),
    'host': ('Имя компьютера', 'Computer name', 'Computername', 'Nom de l’ordinateur', 'Nombre del equipo',
             'Nome del computer', 'Nome do computador'),
    'none': ('Активных сетевых адаптеров с IPv4-адресом не найдено.', 'No active network adapter with an IPv4 address.',
             'Kein aktiver Netzwerkadapter mit IPv4-Adresse.', 'Aucun adaptateur réseau actif avec une adresse IPv4.',
             'No hay adaptadores de red activos con dirección IPv4.', 'Nessuna scheda di rete attiva con indirizzo IPv4.',
             'Nenhum adaptador de rede ativo com endereço IPv4.'),
    'speed': ('{n} Мбит/с', '{n} Mbit/s', '{n} Mbit/s', '{n} Mbit/s', '{n} Mbit/s', '{n} Mbit/s', '{n} Mbit/s'),
    'partial': ('Часть данных недоступна: {parts}.', 'Some data is unavailable: {parts}.', 'Einige Daten fehlen: {parts}.',
                'Certaines données sont indisponibles : {parts}.', 'Algunos datos no están disponibles: {parts}.',
                'Alcuni dati non sono disponibili: {parts}.', 'Alguns dados não estão disponíveis: {parts}.'),
    'addresses': ('адреса компьютера', 'computer addresses', 'Rechneradressen', 'adresses de l’ordinateur',
                  'direcciones del equipo', 'indirizzi del computer', 'endereços do computador'),
    'interfaces': ('адаптеры', 'adapters', 'Adapter', 'adaptateurs', 'adaptadores', 'schede', 'adaptadores'),
    'stats': ('состояние адаптеров', 'adapter status', 'Adapterstatus', 'état des adaptateurs', 'estado de adaptadores',
              'stato delle schede', 'estado dos adaptadores'),
}


def _t(key: str, code: str, **values: Any) -> str:
    row = _T[key]
    return row[_CODES.index(code) if code in _CODES else 1].format(**values)


def _ipv4(rows: Any) -> list[str]:
    out: list[str] = []
    for row in rows or []:
        family = getattr(row, 'family', None)
        address = str(getattr(row, 'address', '') or '')
        if family != socket.AF_INET or not address:
            continue                                  # MAC (AF_LINK/AF_PACKET) and IPv6 are not shown
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if ip.is_loopback or ip.is_link_local:
            continue
        out.append(address)
    return out


def summary(data: dict[str, Any], code: str = 'ru', *, limit: int = 6) -> str:
    lines = [_t('title', code) + ':']
    host = str(data.get('hostname', '') or '').strip()
    if host:
        lines.append(f"• {_t('host', code)}: {host}")
    interfaces = data.get('interfaces') if isinstance(data.get('interfaces'), dict) else {}
    stats = data.get('stats') if isinstance(data.get('stats'), dict) else {}
    shown = 0
    for name in sorted(interfaces):
        state = stats.get(name)
        if state is not None and not bool(getattr(state, 'isup', True)):
            continue
        addresses = _ipv4(interfaces.get(name))
        if not addresses:
            continue
        speed = int(getattr(state, 'speed', 0) or 0) if state is not None else 0
        tail = f" · {_t('speed', code, n=speed)}" if speed > 0 else ''
        lines.append(f"• {name}: {', '.join(addresses[:2])}{tail}")
        shown += 1
        if shown >= limit:
            break
    if not shown and 'interfaces' in data:
        lines.append(_t('none', code))
    missing = data.get('missing') if isinstance(data.get('missing'), dict) else {}
    if missing:
        parts = ', '.join(_t(key, code) for key in ('addresses', 'interfaces', 'stats') if key in missing)
        lines.append(_t('partial', code, parts=parts or ', '.join(sorted(missing))))
    return '\n'.join(lines)
