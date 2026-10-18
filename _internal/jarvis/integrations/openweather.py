# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import requests

from jarvis.language import language_code
from jarvis.secrets import unprotect_secret

_LANG = {"ru":"ru","en":"en","de":"de","fr":"fr","es":"es","it":"it","pt":"pt"}


@dataclass(frozen=True)
class GeoPoint:
    name: str
    country: str
    lat: float
    lon: float


_WORD_NUMBERS = {
    "ноль":0,"один":1,"одна":1,"одно":1,"два":2,"две":2,"три":3,"четыре":4,"пять":5,"шесть":6,"семь":7,"восемь":8,"девять":9,"десять":10,"одиннадцать":11,"двенадцать":12,
    "one":1,"two":2,"three":3,"four":4,"five":5,"six":6,"seven":7,"eight":8,"nine":9,"ten":10,"eleven":11,"twelve":12,
    "eins":1,"zwei":2,"drei":3,"vier":4,"fünf":5,"funf":5,"sechs":6,
    "un":1,"une":1,"deux":2,"trois":3,"quatre":4,"cinq":5,"six":6,
    "uno":1,"una":1,"dos":2,"tres":3,"cuatro":4,"cinco":5,"seis":6,
    "due":2,"tre":3,"quattro":4,"cinque":5,"sei":6,
    "um":1,"uma":1,"dois":2,"duas":2,"três":3,"tres":3,"quatro":4,"cinco":5,"seis":6,
}


class OpenWeatherAdapter:
    """OpenWeather REST adapter with exact relative-time handling.

    The regular 5-day forecast has a 3-hour step. If the user's key has One Call
    3.x enabled, JARVIS transparently uses its hourly data for requests like
    "через два часа"; otherwise it chooses the nearest 3-hour forecast point and
    clearly says that it is the nearest available point rather than fabricating
    an exact hourly measurement.
    """

    def __init__(self, config: Any) -> None:
        self.config = config
        self._geo_cache: dict[str, tuple[float, GeoPoint]] = {}
        self._payload_cache: dict[tuple[str, float, float], tuple[float, dict[str, Any]]] = {}
        from jarvis.runtime import http_pool173
        self.session = http_pool173.session("weather")      # NEO 173: shared, warmed up at the wake word
        self.session.headers.update({"User-Agent": "NEO-JARVIS/4.0.20"})
        self._onecall_unavailable_until = 0.0
        self._api_key_cache: tuple[str, str] = ("", "")

    @staticmethod
    def _bounded_cache_put(cache: dict[Any, Any], key: Any, value: Any, *, limit: int) -> None:
        # Weather locations are user-supplied. Keep long-running voice sessions
        # from retaining an unbounded set of cities/forecast payloads. Dicts are
        # insertion ordered on supported Python versions, so discard oldest first.
        cache[key] = value
        while len(cache) > max(8, int(limit)):
            try:
                cache.pop(next(iter(cache)))
            except (StopIteration, KeyError):
                break

    def _key(self) -> str:
        encrypted = str(self.config.get("integrations.openweather.api_key_encrypted", "") or "")
        source, cached = self._api_key_cache
        if source == encrypted:
            key = cached
        else:
            key = unprotect_secret(encrypted).strip()
            self._api_key_cache = (encrypted, key)
        if not key:
            raise RuntimeError("OpenWeather API key is not configured. Add it in Settings -> Advanced -> API keys.")
        return key

    def _units(self) -> str:
        value = str(self.config.get("integrations.openweather.units", "metric") or "metric").casefold()
        return value if value in {"metric", "imperial", "standard"} else "metric"

    def _lang(self) -> str:
        return _LANG.get(language_code(self.config), "en")

    def _get(self, url: str, params: dict[str, Any], *, soft: bool = False) -> dict[str, Any] | None:
        try:
            response = self.session.get(url, params=params, timeout=12)
            payload = response.json()
            if response.status_code >= 400:
                if soft:
                    return None
                message = str(payload.get("message", "") or f"HTTP {response.status_code}")
                raise RuntimeError(f"OpenWeather: {message}")
            return payload
        except RuntimeError:
            raise
        except requests.RequestException as exc:
            if soft:
                return None
            raise RuntimeError("OpenWeather: сеть временно недоступна; сохранённые настройки не изменены") from exc
        except Exception as exc:
            if soft:
                return None
            raise RuntimeError("OpenWeather returned invalid data") from exc

    def on_network_changed(self, *_args: Any) -> None:
        previous = self.session
        from jarvis.runtime import http_pool173
        self.session = http_pool173.reset("weather")
        previous.close()
        # TTL remains wall-clock based. The regular UI scheduler refreshes stale
        # entries; do not add another polling timer on a network transition.

    @staticmethod
    def _city_query_candidates(city: str) -> list[str]:
        """Return conservative geocoding variants for spoken Russian cases.

        We always query the user's exact city first. Only when OpenWeather does
        not recognize a locative form ("в Москве", "в Новосибирске") do we try
        a base-form candidate. This never falls back to the configured home city.
        """
        raw = str(city or "").strip()
        if not raw:
            return []
        candidates = [raw]
        known = {
            "москве": "Москва", "киеве": "Киев", "минске": "Минск",
            "санкт петербурге": "Санкт Петербург", "санкт-петербурге": "Санкт-Петербург",
            "париже": "Париж", "лондоне": "Лондон", "берлине": "Берлин",
            "риме": "Рим", "самаре": "Самара", "астане": "Астана",
            "алматы": "Алматы", "тбилиси": "Тбилиси", "баку": "Баку",
        }
        low = raw.casefold().replace("ё", "е")
        if low in known:
            candidates.append(known[low])
        else:
            words = raw.split()
            if words:
                last = words[-1]
                ll = last.casefold().replace("ё", "е")
                base = ""
                if ll.endswith("бурге") and len(last) > 5:
                    base = last[:-1]
                elif ll.endswith("граде") and len(last) > 5:
                    base = last[:-1]
                elif ll.endswith("ске") and len(last) > 4:
                    base = last[:-1]
                elif ll.endswith("ине") and len(last) > 4:
                    base = last[:-1]
                elif ll.endswith("оне") and len(last) > 4:
                    base = last[:-1]
                elif ll.endswith("же") and len(last) > 3:
                    base = last[:-1]
                if base:
                    candidates.append(" ".join([*words[:-1], base]))
        # Keep order, avoid duplicate requests.
        return list(dict.fromkeys(x for x in candidates if x))

    def geocode(self, city: str = "", country: str = "") -> GeoPoint:
        city = str(city or self.config.get("integrations.openweather.city", "") or "").strip()
        country = str(country or self.config.get("integrations.openweather.country_code", "") or "").strip().upper()
        if not city:
            raise RuntimeError("Weather city is not configured. Set it in Settings -> Advanced -> API keys.")
        cache_key = f"{city.casefold()}|{country}"
        cached = self._geo_cache.get(cache_key)
        if cached and time.time() - cached[0] < 86400:
            return cached[1]
        q = city + (f",{country}" if country else "")
        rows: Any = []
        matched_query = q
        for candidate in self._city_query_candidates(city):
            candidate_q = candidate + (f",{country}" if country else "")
            rows = self._get("https://api.openweathermap.org/geo/1.0/direct", {"q":candidate_q,"limit":5,"appid":self._key()})
            if isinstance(rows, list) and rows:
                matched_query = candidate_q
                break
        if not isinstance(rows, list) or not rows:
            raise RuntimeError(f"OpenWeather could not find requested location: {q}")
        row = rows[0]
        point = GeoPoint(str((row.get("local_names") or {}).get(self._lang()) or row.get("name") or city), str(row.get("country") or country), float(row.get("lat")), float(row.get("lon")))
        self._bounded_cache_put(self._geo_cache, cache_key, (time.time(), point), limit=64)
        return point

    def _cached_endpoint(self, kind: str, point: GeoPoint) -> dict[str, Any]:
        key=(kind,round(point.lat,4),round(point.lon,4),self._units(),self._lang()); cached=self._payload_cache.get(key)
        ttl=240 if kind=="current" else 420
        if cached and time.time()-cached[0]<ttl: return cached[1]
        endpoint="weather" if kind=="current" else "forecast"
        payload=self._get(f"https://api.openweathermap.org/data/2.5/{endpoint}",{"lat":point.lat,"lon":point.lon,"appid":self._key(),"units":self._units(),"lang":self._lang()})
        assert isinstance(payload,dict)
        payload = {**payload, "_neo_fetched_at": time.time()}
        self._bounded_cache_put(self._payload_cache, key, (time.time(), payload), limit=96); return payload

    def current(self, city: str = "", country: str = "") -> dict[str, Any]:
        point=self.geocode(city,country); data=self._cached_endpoint("current",point)
        main=data.get("main") or {}; wind=data.get("wind") or {}; weather=(data.get("weather") or [{}])[0] or {}; sys=data.get("sys") or {}
        return {"city":point.name,"country":point.country,"lat":point.lat,"lon":point.lon,"temperature":main.get("temp"),"feels_like":main.get("feels_like"),"humidity":main.get("humidity"),"pressure_hpa":main.get("pressure"),"visibility_m":data.get("visibility"),"wind_speed":wind.get("speed"),"wind_deg":wind.get("deg"),"clouds_percent":(data.get("clouds") or {}).get("all"),"description":weather.get("description") or weather.get("main") or "","rain_1h_mm":(data.get("rain") or {}).get("1h",0.0),"snow_1h_mm":(data.get("snow") or {}).get("1h",0.0),"sunrise":sys.get("sunrise"),"sunset":sys.get("sunset"),"timezone_offset":data.get("timezone",0),"units":self._units()}

    def forecast(self, city: str = "", country: str = "") -> dict[str, Any]:
        point=self.geocode(city,country); data=self._cached_endpoint("forecast",point); rows=[]
        for raw in data.get("list") or []:
            main=raw.get("main") or {}; weather=(raw.get("weather") or [{}])[0] or {}
            rows.append({"timestamp":int(raw.get("dt") or 0),"temperature":main.get("temp"),"feels_like":main.get("feels_like"),"humidity":main.get("humidity"),"pressure_hpa":main.get("pressure"),"description":weather.get("description") or weather.get("main") or "","rain_probability":float(raw.get("pop") or 0.0),"rain_mm":float((raw.get("rain") or {}).get("3h",0.0) or 0.0),"snow_mm":float((raw.get("snow") or {}).get("3h",0.0) or 0.0),"wind_speed":(raw.get("wind") or {}).get("speed"),"clouds_percent":(raw.get("clouds") or {}).get("all"),"source":"3h"})
        return {"city":point.name,"country":point.country,"timezone_offset":int((data.get("city") or {}).get("timezone",0) or 0),"rows":rows,"units":self._units()}

    def onecall_hourly(self, city: str = "", country: str = "") -> dict[str, Any] | None:
        # One Call is optional for the user. Remember a rejected/unavailable key
        # for 30 minutes so ordinary 5-day forecast users do not pay a failing
        # HTTP round-trip on every relative-hour question.
        if time.time() < self._onecall_unavailable_until:
            return None
        point=self.geocode(city,country); key=("onecall",round(point.lat,4),round(point.lon,4)); cached=self._payload_cache.get(key)
        if cached and time.time()-cached[0]<420: data=cached[1]
        else:
            data=self._get("https://api.openweathermap.org/data/3.0/onecall",{"lat":point.lat,"lon":point.lon,"exclude":"minutely,daily,alerts","appid":self._key(),"units":self._units(),"lang":self._lang()},soft=True)
            if not isinstance(data,dict) or not isinstance(data.get("hourly"),list):
                self._onecall_unavailable_until = time.time() + 1800
                return None
            self._bounded_cache_put(self._payload_cache, key, (time.time(), data), limit=96)
        rows=[]
        for raw in data.get("hourly") or []:
            weather=(raw.get("weather") or [{}])[0] or {}
            rows.append({"timestamp":int(raw.get("dt") or 0),"temperature":raw.get("temp"),"feels_like":raw.get("feels_like"),"humidity":raw.get("humidity"),"pressure_hpa":raw.get("pressure"),"description":weather.get("description") or weather.get("main") or "","rain_probability":float(raw.get("pop") or 0.0),"rain_mm":float((raw.get("rain") or {}).get("1h",0.0) or 0.0),"snow_mm":float((raw.get("snow") or {}).get("1h",0.0) or 0.0),"wind_speed":raw.get("wind_speed"),"clouds_percent":raw.get("clouds"),"source":"hourly"})
        return {"city":point.name,"country":point.country,"timezone_offset":int(data.get("timezone_offset",0) or 0),"rows":rows,"units":self._units()}

    @staticmethod
    def _local_dt(timestamp: int, offset: int) -> datetime:
        return datetime.utcfromtimestamp(int(timestamp)) + timedelta(seconds=int(offset or 0))

    @staticmethod
    def _relative_hours(period: str) -> float | None:
        p=str(period or "").casefold().replace(",",".")
        direct=re.search(r"(?:через|in|nach|dans|en|tra|fra|daqui\s+a)\s+(\d+(?:\.\d+)?)\s*(?:час|ч\b|hours?|hrs?|stunden?|heures?|horas?|ore|hora)",p,re.I)
        if direct: return float(direct.group(1))
        direct_min=re.search(r"(?:через|in|nach|dans|en|tra|fra|daqui\s+a)\s+(\d+(?:\.\d+)?)\s*(?:минут|мин\b|minutes?|mins?|minuten?|minutos?|minuti)",p,re.I)
        if direct_min: return float(direct_min.group(1))/60.0
        for word,num in _WORD_NUMBERS.items():
            if re.search(rf"(?:через|in|nach|dans|en|tra|fra|daqui\s+a)\s+{re.escape(word)}\s*(?:час|hours?|stunden?|heures?|horas?|ore|hora)",p,re.I): return float(num)
        return None

    def _relative_point(self, period: str, city: str, country: str) -> dict[str, Any] | None:
        hours=self._relative_hours(period)
        if hours is None or hours < 0 or hours > 120: return None
        basic=self.forecast(city,country); offset=basic["timezone_offset"]
        now=datetime.utcnow()+timedelta(seconds=offset); target=now+timedelta(hours=hours)
        source=self.onecall_hourly(city,country) if hours <= 48 else None
        source=source or basic; offset=source["timezone_offset"]
        rows=source.get("rows") or []
        if not rows: return None
        chosen=min(rows,key=lambda row:abs((self._local_dt(row["timestamp"],offset)-target).total_seconds()))
        chosen=dict(chosen); chosen["local_dt"]=self._local_dt(chosen["timestamp"],offset).isoformat(timespec="minutes")
        chosen["requested_hours"]=hours; chosen["target_local"]=target.isoformat(timespec="minutes"); chosen["nearest_delta_minutes"]=round(abs((self._local_dt(chosen["timestamp"],offset)-target).total_seconds())/60)
        return {**source,"relative":True,"selected":[chosen],"requested_hours":hours}

    def select_period(self, period: str = "", city: str = "", country: str = "") -> dict[str, Any]:
        relative=self._relative_point(period,city,country)
        if relative is not None: return relative
        forecast=self.forecast(city,country); rows=forecast["rows"]; offset=forecast["timezone_offset"]; now=datetime.utcnow()+timedelta(seconds=offset); p=str(period or "").casefold().strip()
        day_offset=1 if any(x in p for x in ("завтра","tomorrow","morgen","demain","mañana","domani","amanhã")) else (2 if any(x in p for x in ("послезавтра","day after tomorrow","übermorgen","après-demain","pasado mañana","dopodomani","depois de amanhã")) else 0)
        target_date=(now+timedelta(days=day_offset)).date()
        if any(x in p for x in ("вечер","evening","abend","soir","sera")): start_h,end_h=17,23
        elif any(x in p for x in ("утр","morning","morgen früh","matin","mattina","manhã")): start_h,end_h=5,12
        elif any(x in p for x in ("ноч","night","nacht","nuit","noche","notte","noite")): start_h,end_h=0,6
        elif any(x in p for x in ("днем","днём","afternoon","nachmittag","après-midi","pomeriggio")): start_h,end_h=12,18
        else: start_h,end_h=0,24
        selected=[]
        for row in rows:
            dt=self._local_dt(row["timestamp"],offset)
            if dt.date()==target_date and start_h<=dt.hour<end_h: selected.append(dict(row,local_dt=dt.isoformat(timespec="minutes")))
        if not selected and day_offset==0:
            for row in rows:
                dt=self._local_dt(row["timestamp"],offset)
                if now<=dt<=now+timedelta(hours=12): selected.append(dict(row,local_dt=dt.isoformat(timespec="minutes")))
        return {**forecast,"period":p,"selected":selected,"relative":False}

    def answer(self, action: str, *, period: str = "", city: str = "", country: str = "") -> tuple[str, dict[str, Any]]:
        action = str(action or "summary").casefold()
        current = self.current(city, country)
        lang = self._lang()
        if lang not in {"ru", "en", "de", "fr", "es", "it", "pt"}:
            lang = "en"
        labels = {
            "ru": {"weather":"Погода","now":"Сейчас","feels":"Ощущается","conditions":"Условия","humidity":"Влажность","wind":"Ветер","direction":"Направление","sunrise":"Восход","sunset":"Закат","temp":"Температура","precip":"Осадки","prob":"Вероятность","amount":"Количество","likely":"вероятны","unlikely":"маловероятны","source":"Точность","nearest":"ближайшая 3-часовая точка","in":"Через","no_point":"Нет подходящей точки прогноза для указанного времени.","range":"Период","up_to":"до","about":"около"},
            "en": {"weather":"Weather","now":"Now","feels":"Feels like","conditions":"Conditions","humidity":"Humidity","wind":"Wind","direction":"Direction","sunrise":"Sunrise","sunset":"Sunset","temp":"Temperature","precip":"Precipitation","prob":"Probability","amount":"Amount","likely":"likely","unlikely":"unlikely","source":"Precision","nearest":"nearest 3-hour point","in":"In","no_point":"No forecast point is available for that time.","range":"Period","up_to":"up to","about":"about"},
            "de": {"weather":"Wetter","now":"Jetzt","feels":"Gefühlt","conditions":"Bedingungen","humidity":"Luftfeuchtigkeit","wind":"Wind","direction":"Richtung","sunrise":"Sonnenaufgang","sunset":"Sonnenuntergang","temp":"Temperatur","precip":"Niederschlag","prob":"Wahrscheinlichkeit","amount":"Menge","likely":"wahrscheinlich","unlikely":"unwahrscheinlich","source":"Genauigkeit","nearest":"nächster 3-Stunden-Punkt","in":"In","no_point":"Für diese Zeit ist kein passender Vorhersagepunkt verfügbar.","range":"Zeitraum","up_to":"bis","about":"etwa"},
            "fr": {"weather":"Météo","now":"Maintenant","feels":"Ressenti","conditions":"Conditions","humidity":"Humidité","wind":"Vent","direction":"Direction","sunrise":"Lever du soleil","sunset":"Coucher du soleil","temp":"Température","precip":"Précipitations","prob":"Probabilité","amount":"Quantité","likely":"probables","unlikely":"peu probables","source":"Précision","nearest":"point de prévision 3 h le plus proche","in":"Dans","no_point":"Aucun point de prévision n'est disponible pour cette heure.","range":"Période","up_to":"jusqu'à","about":"environ"},
            "es": {"weather":"Tiempo","now":"Ahora","feels":"Sensación","conditions":"Condiciones","humidity":"Humedad","wind":"Viento","direction":"Dirección","sunrise":"Amanecer","sunset":"Atardecer","temp":"Temperatura","precip":"Precipitación","prob":"Probabilidad","amount":"Cantidad","likely":"probable","unlikely":"poco probable","source":"Precisión","nearest":"punto de 3 horas más cercano","in":"En","no_point":"No hay un punto de pronóstico disponible para esa hora.","range":"Periodo","up_to":"hasta","about":"aprox."},
            "it": {"weather":"Meteo","now":"Adesso","feels":"Percepita","conditions":"Condizioni","humidity":"Umidità","wind":"Vento","direction":"Direzione","sunrise":"Alba","sunset":"Tramonto","temp":"Temperatura","precip":"Precipitazioni","prob":"Probabilità","amount":"Quantità","likely":"probabili","unlikely":"poco probabili","source":"Precisione","nearest":"punto a 3 ore più vicino","in":"Tra","no_point":"Non è disponibile un punto di previsione per quell'ora.","range":"Periodo","up_to":"fino a","about":"circa"},
            "pt": {"weather":"Tempo","now":"Agora","feels":"Sensação","conditions":"Condições","humidity":"Humidade","wind":"Vento","direction":"Direção","sunrise":"Nascer do sol","sunset":"Pôr do sol","temp":"Temperatura","precip":"Precipitação","prob":"Probabilidade","amount":"Quantidade","likely":"provável","unlikely":"pouco provável","source":"Precisão","nearest":"ponto de 3 horas mais próximo","in":"Daqui a","no_point":"Não há ponto de previsão disponível para essa hora.","range":"Período","up_to":"até","about":"cerca de"},
        }[lang]
        unit = "°C" if current["units"] == "metric" else ("°F" if current["units"] == "imperial" else "K")
        wind_unit = "mph" if current["units"] == "imperial" else ("м/с" if lang == "ru" else "m/s")
        rain_unit = "мм" if lang == "ru" else "mm"
        minute_unit = "мин" if lang == "ru" else "min"
        loc = current["city"] + (f", {current['country']}" if current.get("country") else "")
        temp = float(current.get("temperature") or 0)
        feels = float(current.get("feels_like") if current.get("feels_like") is not None else temp)
        humidity = current.get("humidity")
        wind = current.get("wind_speed")
        desc = str(current.get("description") or "—")

        def val(v: Any, digits: int = 1) -> str:
            try:
                return f"{float(v):.{digits}f}"
            except Exception:
                return "—"

        # The router passes the full utterance in `period` for every weather
        # command. Only treat it as a forecast selector when it actually contains
        # a future/relative/daypart expression; otherwise "какая сейчас погода"
        # must stay on current conditions rather than accidentally aggregating the
        # next 12 hours.
        p = str(period or "").casefold()
        future_period = self._relative_hours(p) is not None or any(x in p for x in (
            "завтра", "послезавтра", "вечер", "утр", "ноч", "днем", "днём",
            "tomorrow", "day after tomorrow", "evening", "morning", "tonight", "afternoon",
            "morgen", "übermorgen", "abend", "nacht", "demain", "soir", "matin", "nuit",
            "mañana", "noche", "domani", "sera", "notte", "amanhã", "amanha", "noite",
        ))

        if action in {"current", "temperature", "summary"} and not future_period:
            lines = [
                f"{labels['weather']} — {loc}",
                f"• {labels['now']}: {val(temp)} {unit}",
                f"• {labels['feels']}: {val(feels)} {unit}",
                f"• {labels['conditions']}: {desc.capitalize()}",
                f"• {labels['humidity']}: {humidity if humidity is not None else '—'}%",
                f"• {labels['wind']}: {val(wind)} {wind_unit}",
            ]
            return "\n".join(lines), current
        if action == "humidity" and not future_period:
            return f"{labels['weather']} — {loc}\n• {labels['humidity']}: {humidity if humidity is not None else '—'}%", current
        if action == "wind" and not future_period:
            return (f"{labels['weather']} — {loc}\n"
                    f"• {labels['wind']}: {val(wind)} {wind_unit}\n"
                    f"• {labels['direction']}: {current.get('wind_deg', '—')}°"), current
        if action in {"sunrise", "sunset"}:
            ts = current.get(action)
            if not ts:
                return f"{labels['weather']} — {loc}\n• {labels[action]}: —", current
            local = self._local_dt(int(ts), int(current.get("timezone_offset", 0) or 0))
            return f"{labels['weather']} — {loc}\n• {labels[action]}: {local:%H:%M}", current

        selected = self.select_period(period, city, country)
        rows = selected["selected"]
        if not rows:
            return f"{labels['weather']} — {loc}\n• {labels['no_point']}", selected

        if selected.get("relative"):
            r = rows[0]
            dt = datetime.fromisoformat(r["local_dt"])
            pct = round(float(r.get("rain_probability") or 0) * 100)
            requested = float(selected.get("requested_hours") or 0)
            delta = int(r.get("nearest_delta_minutes") or 0)
            request_label = f"{labels['in']} {requested:g} {'ч' if lang == 'ru' else 'h'} — {dt:%H:%M}"
            lines = [f"{labels['weather']} — {loc}", request_label]
            if action in {"rain", "precipitation"}:
                rain = float(r.get("rain_mm") or 0)
                likely = pct >= 35 or rain > 0
                lines.extend([
                    f"• {labels['precip']}: {labels['likely'] if likely else labels['unlikely']}",
                    f"• {labels['prob']}: {pct}%",
                    f"• {labels['amount']}: {rain:.1f} {rain_unit}",
                ])
            else:
                lines.extend([
                    f"• {labels['temp']}: {val(r.get('temperature'))} {unit}",
                    f"• {labels['feels']}: {val(r.get('feels_like') if r.get('feels_like') is not None else r.get('temperature'))} {unit}",
                    f"• {labels['conditions']}: {str(r.get('description') or '—').capitalize()}",
                    f"• {labels['precip']}: {pct}% · {float(r.get('rain_mm') or 0):.1f} {rain_unit}",
                    f"• {labels['wind']}: {val(r.get('wind_speed'))} {wind_unit}",
                ])
            if r.get("source") == "3h":
                lines.append(f"• {labels['source']}: {labels['nearest']} (±{delta} {minute_unit})")
            return "\n".join(lines), selected

        temps = [float(r["temperature"]) for r in rows if r.get("temperature") is not None]
        rain_prob = max(float(r.get("rain_probability") or 0) for r in rows)
        rain_mm = sum(float(r.get("rain_mm") or 0) for r in rows)
        descriptions = [str(r.get("description") or "") for r in rows if r.get("description")]
        common_desc = max(descriptions, key=descriptions.count) if descriptions else "—"
        pct = round(rain_prob * 100)
        first_dt = datetime.fromisoformat(rows[0]["local_dt"])
        last_dt = datetime.fromisoformat(rows[-1]["local_dt"])
        when = f"{first_dt:%d.%m %H:%M}–{last_dt:%H:%M}"
        lines = [f"{labels['weather']} — {loc}", f"• {labels['range']}: {when}"]
        if action in {"rain", "precipitation"}:
            likely = rain_prob >= 0.35 or rain_mm > 0
            lines.extend([
                f"• {labels['precip']}: {labels['likely'] if likely else labels['unlikely']}",
                f"• {labels['prob']}: {labels['up_to']} {pct}%",
                f"• {labels['amount']}: {labels['about']} {rain_mm:.1f} {rain_unit}",
            ])
            return "\n".join(lines), selected
        if not temps:
            return f"{labels['weather']} — {loc}\n• {labels['no_point']}", selected
        low, high = min(temps), max(temps)
        lines.extend([
            f"• {labels['temp']}: {low:.1f}…{high:.1f} {unit}",
            f"• {labels['conditions']}: {common_desc.capitalize()}",
            f"• {labels['precip']}: {labels['up_to']} {pct}%",
        ])
        return "\n".join(lines), selected
