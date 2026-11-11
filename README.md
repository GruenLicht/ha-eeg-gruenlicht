<img src="custom_components/eeg_gruenlicht/brand/icon.png" alt="EEG GrünLicht" width="96" align="right">

# EEG GrünLicht für Home Assistant

Mit dieser Integration verbindest du dein Home Assistant mit deinem Mitgliedskonto bei der Erneuerbare-Energie-Gemeinschaft **EEG Grünes Licht**. Home Assistant zeigt dann deine Mitgliedsdaten an und übernimmt die Viertelstundenwerte der EEG in seine Statistik, zum Beispiel für das Energie-Dashboard.

Die Teilnahme ist freiwillig. Die Verbindung geht immer von deinem Home Assistant aus nach außen, eine Portfreigabe am Router ist nicht nötig.

## Was die Integration kann

| Stufe | Inhalt | Status |
| --- | --- | --- |
| 1 | Kopplung mit dem Mitgliedskonto; Energiegemeinschaft, Mitgliedsnummer, Rolle, Zählpunkte und Verbindungsstatus als Sensoren | verfügbar |
| 2 | Viertelstundenwerte der EEG als Langzeitstatistik: Verbrauch, Bezug aus der EEG, Bezug aus dem Netz, Erzeugung, Einspeisung in die EEG, Einspeisung ins Netz | verfügbar, abschaltbar |
| 3 | Geräte (Wechselrichter, Batterie, Wallbox, Wärmepumpe) nach Freigabe an die EEG melden | geplant |
| 4 | Fahrplan der EEG für die Gerätesteuerung | geplant |

## Voraussetzungen

- Home Assistant 2024.11 oder neuer, empfohlen ist die aktuelle Version
- ein Mitgliedskonto im Mitgliederportal der EEG Grünes Licht

## Installation

### Über den Marketplace (Home Assistant 2026.11 und neuer)

1. Öffne in Home Assistant den **Marketplace** in der Seitenleiste.
2. Wähle oben rechts **Add from link** und gib diese Adresse ein: `https://github.com/GruenLicht/ha-eeg-gruenlicht`
3. Öffne „EEG GrünLicht“ und klicke **Install**.
4. Starte Home Assistant neu: **Settings → System → Restart**.

### Über HACS (ältere Versionen von Home Assistant)

1. Öffne **HACS**, dann das Menü **⋮ → Custom repositories**.
2. Gib als Repository `https://github.com/GruenLicht/ha-eeg-gruenlicht` ein, als Typ **Integration**, und klicke **Add**.
3. Suche in HACS nach „EEG GrünLicht“ und klicke **Download**.
4. Starte Home Assistant neu.

### Von Hand

Kopiere den Ordner `custom_components/eeg_gruenlicht` aus diesem Repository nach `/config/custom_components/` in deinem Home Assistant und starte Home Assistant neu.

## Einrichtung

1. Öffne in Home Assistant **Settings → Devices & services → Add integration** und wähle **EEG GrünLicht**.
2. Öffne in einem zweiten Fenster das Mitgliederportal der EEG und dort **App verbinden**. Unter dem QR-Code steht ein 8-stelliger **Kurzcode**.
3. Gib den Kurzcode in Home Assistant ein und klicke **Submit**.
4. Home Assistant zeigt einen 6-stelligen Code. Im Portal erscheint gleichzeitig eine Anfrage von „Home Assistant“. Wenn dort derselbe Code steht, bestätige sie. Dafür hast du 2 Minuten.
5. Fertig: Es erscheint das Gerät **EEG GrünLicht** mit deinen Daten.

Die Viertelstundenwerte werden danach im Hintergrund geladen. Beim ersten Mal holt die Integration bis zu ein Jahr nach, das dauert einige Minuten. Wie weit sie ist, zeigt der Sensor **Viertelstundenwerte bis**.

## Viertelstundenwerte im Energie-Dashboard

Die Werte stehen als Statistiken mit dem Namen „EEG GrünLicht …“ zur Verfügung. Du findest sie unter **Settings → Dashboards → Energy** in den Auswahllisten und in der Karte **Statistics graph**.

Gut zu wissen:

- Die EEG bekommt die Messwerte vom Netzbetreiber erst am Folgetag. Die Statistik reicht deshalb immer nur bis gestern oder vorgestern.
- Home Assistant speichert Statistik stündlich. Je vier Viertelstunden werden zu einer Stunde zusammengefasst.
- Die letzten 3 Tage werden bei jedem Lauf neu geladen, weil der Netzbetreiber Werte manchmal nachträglich korrigiert.

## Freigaben ändern und Verbindung trennen

- Unter **Settings → Devices & services → EEG GrünLicht → Configure** schaltest du den Import der Viertelstundenwerte ein oder aus.
- Entfernst du die Integration, meldet Home Assistant die Verbindung bei der EEG ab.
- Im Mitgliederportal siehst du die Verbindung „Home Assistant“ in deiner Geräteliste und kannst sie dort jederzeit trennen. Home Assistant fragt dann nach einem neuen Kurzcode.

## Datenschutz

- Home Assistant holt nur die Daten deines eigenen Mitgliedskontos. Sie bleiben in deinem Home Assistant.
- An die EEG geht in Stufe 1 und 2 nur, was für die Verbindung nötig ist: ein eigener Geräteschlüssel, die Gerätebezeichnung „Home Assistant (EEG GrünLicht \<Version\>)“ und der Zeitpunkt der letzten Abfrage. Die EEG sieht dadurch, welche Mitglieder die Integration verwenden.
- Die Verbindung ist an einen Schlüssel gebunden, der nur in deinem Home Assistant liegt. Ein abgefangener Zugangstoken allein nützt einem Angreifer nichts.

## Entwicklung

```
pip install pytest-homeassistant-custom-component
pytest
```

## Lizenz

[Apache License 2.0](LICENSE)
