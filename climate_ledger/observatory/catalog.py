from decimal import Decimal

from ..contracts import (contains, day, distinct_strings, identifier, integer, number,
                         overlaps, period, require, shape, text)


UNITS = {"temperature": {"C", "F"}, "water": {"L", "m3"}, "energy": {"kWh", "MWh"}}
CANONICAL_UNITS = {"temperature": "C", "water": "L", "energy": "kWh"}


def normalize_zone(record):
    shape(record, ("id", "revision", "name"))
    return {"id": identifier(record["id"]), "revision": integer(record["revision"], "revision", 1),
            "name": text(record["name"], "name")}


def normalize_metric(record):
    shape(record, ("id", "revision", "name", "dimension", "unit", "threshold", "direction", "minimum", "maximum"))
    dimension = record["dimension"]
    require(dimension in UNITS, "dimension", "不支持的指标量纲")
    require(record["unit"] == CANONICAL_UNITS[dimension], "unit", "指标阈值使用标准单位")
    require(record["direction"] in ("high", "low"), "direction", "异常方向需要high或low")
    minimum = number(record["minimum"], "minimum")
    maximum = number(record["maximum"], "maximum")
    threshold = number(record["threshold"], "threshold")
    require(minimum < maximum and minimum <= threshold <= maximum, "range", "指标阈值与有效值域不一致")
    return {"id": identifier(record["id"]), "revision": integer(record["revision"], "revision", 1),
            "name": text(record["name"], "name"), "dimension": dimension, "unit": record["unit"],
            "threshold": str(threshold), "direction": record["direction"],
            "minimum": str(minimum), "maximum": str(maximum)}


def normalize_station(record):
    shape(record, ("id", "revision", "zone", "start", "end", "weight", "metrics"))
    start, end = period(record["start"], record["end"])
    return {"id": identifier(record["id"]), "revision": integer(record["revision"], "revision", 1),
            "zone": identifier(record["zone"], "zone"), "start": start, "end": end,
            "weight": str(number(record["weight"], "weight", "0.000001", 1)),
            "metrics": distinct_strings(record["metrics"], "metrics")}


class Catalog:
    def __init__(self, snapshot):
        self.zones = snapshot["zone"]
        self.metrics = snapshot["metric"]
        self.stations = snapshot["station"]

    def validate(self):
        for station in self.stations.values():
            require(station["zone"] in self.zones, "reference", "站点行政区不存在")
            require(set(station["metrics"]).issubset(self.metrics), "reference", "站点指标不存在")
        for metric in self.metrics:
            for zone in self.zones:
                selected = [s for s in self.stations.values() if s["zone"] == zone and metric in s["metrics"]]
                boundaries = sorted({s["start"] for s in selected})
                for when in boundaries:
                    active = [s for s in selected if contains(s, when)]
                    total = sum((Decimal(s["weight"]) for s in active), Decimal(0))
                    require(total <= 1, "station_weight", "同区同时有效站点权重不能超过1", zone=zone, metric=metric, day=when)

    def metric(self, metric):
        require(metric in self.metrics, "metric", "指标不存在", metric=metric)
        return self.metrics[metric]

    def station(self, station):
        require(station in self.stations, "station", "站点不存在", station=station)
        return self.stations[station]

    def active_stations(self, zone, metric, when):
        require(zone in self.zones, "zone", "行政区不存在", zone=zone)
        self.metric(metric)
        day(when)
        return [s for s in self.stations.values()
                if s["zone"] == zone and metric in s["metrics"] and contains(s, when)]

    def hazardous(self, metric, value):
        definition = self.metric(metric)
        threshold = Decimal(definition["threshold"])
        return value >= threshold if definition["direction"] == "high" else value <= threshold

    def severity(self, metric, value):
        definition = self.metric(metric)
        threshold = Decimal(definition["threshold"])
        difference = value - threshold if definition["direction"] == "high" else threshold - value
        return max(Decimal(0), difference)
