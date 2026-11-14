from dataclasses import dataclass, field


@dataclass
class Report:
    missing: list[str] = field(default_factory=list)
    corrupt: list[str] = field(default_factory=list)
    orphan: list[str] = field(default_factory=list)
    unreferenced: list[str] = field(default_factory=list)
    stale_temp: list[str] = field(default_factory=list)
    manifest_drift: list[str] = field(default_factory=list)

    @property
    def ok(self):
        return not (self.missing or self.corrupt or self.orphan or self.manifest_drift)
