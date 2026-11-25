"""Russian descriptions of content: effects, and any entity by id or name (`describe`, TECH_AND_SETUP.md 4)."""

from __future__ import annotations

from typing import Any, Mapping

from . import registry
from .content import Ruleset, split_cost

KINDS = ("unit", "building", "tech", "artifact", "faction", "branch", "resource", "province_type", "objective",
         "discovery")
KIND_LABELS = {"unit": "юнит", "building": "здание", "tech": "технология", "artifact": "артефакт",
               "faction": "фракция", "branch": "ветка технологий", "resource": "ресурс",
               "province_type": "тип провинции", "objective": "тайная цель", "discovery": "находка"}
_TABLES = {"unit": "units", "building": "buildings", "tech": "techs", "artifact": "artifacts",
           "faction": "factions", "branch": "branches", "resource": "resources", "province_type": "province_types",
           "objective": "objectives", "discovery": "discoveries"}
# stage 5 (ABANDONED_LANDS.md 3, 4): kinds and rarities of finds
DISCOVERY_KIND_TEXT = {"boon": "плюшка", "hindrance": "помеха", "mixed": "смешанная"}
RARITY_TEXT = {"common": "обычная", "uncommon": "необычная", "rare": "редкая", "minor": "малая", "major": "большая"}

ABILITY_TEXT = {
    "ignore_fortify": "укрепление защитника против него не действует",
    "siege": "оборона провинции против него не выше ×{cap:g}",
    "garrison": "+{defense:g} к обороне в своей провинции",
    "fast": "ходит на {range:g} ребра и по чужой земле",
}
# difficulty tiers of secret objectives (core 0.6.0, registry.OBJECTIVE_DIFFICULTIES)
DIFFICULTY_TEXT = {"easy": "лёгкая", "medium": "средняя", "hard": "трудная"}
_STAT_TEXT = {
    "command_points": "очки командования", "p2.hasty_penalty": "штраф «наспех»",
    "p2.cost.new_attack": "цена новой атаки в П2 (ОК)", "p2.cancel_refund": "возврат резерва при отмене в П2",
    "combat.fortify_bonus": "бонус укрепления",
    "combat.fortify_persists": "укрепление держится, пока армия в провинции", "combat.counter_mul": "множитель контрюнита",
    "starvation.loss_frac": "потери от голода", "province.defense_mul": "оборона провинции",
    "province.recruit_discount": "скидка на найм в провинции",
    "conquest.claim_turns": "срок притязания (ходов)", "conquest.claim_bonus": "бонус атаки по притязанию",
    "conquest.loot_cooldown": "перерыв между добычей с провинции (ходов)",
    # espionage (SPY_AND_CONQUEST.md 1.2)
    "spy.whisper": "Шёпот (S)", "spy.mirrors": "Зеркала (C)", "province.mirrors": "Зеркала в провинции (C_лок)",
    "spy.shades_max": "Теней всего", "spy.shades_free": "Теней даром на старте", "spy.active_base": "Теней на заданиях (база)",
    "spy.rounds_bonus": "срок заданий (раундов)", "spy.detect.army": "армия в провинции к C_лок",
    "spy.attribution.base": "шанс узнать хозяина пойманной Тени", "spy.intel.width": "ширина интервалов донесений",
    "spy.xp_max": "опыт Тени (предел)", "spy.cover_max": "прикрытие Тени (предел)",
    "spy.ward.bonus": "Застава к C_лок", "p2.cost.spy": "цена задания в П2 (ОК)",
    "p2.cost.recall": "цена отзыва в П2 (ОК)", "feint.cp_cost": "цена ложной записи (ОК)",
    # scoring (SCORING.md 2): player-facing labels, never the raw path
    "score.territory.province": "очки за провинцию", "score.territory.capital": "очки за столицу",
    "score.science.tier1": "очки за технологию ур. 1", "score.science.tier2": "очки за технологию ур. 2",
    "score.science.tier3": "очки за технологию ур. 3", "score.science.tier4": "очки за технологию ур. 4+",
    "score.science.branch": "очки за полную ветку",
    "score.war.per_point": "цена убитых за 1 очко войны (меньше — лучше)",
    "score.war.cap_per_victim": "предел очков войны с одного соперника",
    "score.war.cap": "предел очков войны", "score.war.defense_pct": "доля очков войны в обороне, %",
    "score.betrayal.per_break": "штраф очков за нарушенный пакт",
    # core 0.7.0 (decision 27)
    "score.war.province_cap": "предел очков войны с одной провинции",
    "score.war.hold_turns": "ходов удержания за +1 очко войны",
    "score.war.province_total": "предел очков войны по провинциям всего",
    "score.intel.per_success": "очки за успешное задание Тени", "score.intel.cap": "предел очков разведки",
    "fog.army_presence": "видно наличие чужих армий вне обзора",
}


def _name(ruleset: Ruleset, full: str) -> str:
    for kind in KINDS:
        entity = getattr(ruleset, _TABLES[kind]).get(full)
        if entity is not None:
            return entity.name
    return full.split(":")[-1]


def stat_text(ruleset: Ruleset, path: str) -> str:
    """`unit.core:armor.attack` -> «атака: Бронеход» and the like."""
    if path in _STAT_TEXT:
        return _STAT_TEXT[path]
    segs = path.split(".")
    if segs[0] == "unit" and len(segs) >= 3:
        what = {"attack": "атака", "defense": "оборона", "move": "дальность хода", "cost": "цена",
                "upkeep": "содержание", "ability": "способность"}.get(segs[2], segs[2])
        tail = f" {_name(ruleset, segs[3])}" if segs[2] in ("cost", "upkeep") and len(segs) > 3 else ""
        if segs[2] == "ability" and len(segs) == 5:
            tail = f" {segs[3]}" + ("" if segs[4] == "on" else f".{segs[4]}")
        return f"{what}{tail}: {_name(ruleset, segs[1])}"
    if segs[0] == "building" and len(segs) == 3 and segs[2] == "score":
        return f"очки стройки «{_name(ruleset, segs[1])}»"
    if segs[0] == "building" and len(segs) == 4:
        return f"цена {_name(ruleset, segs[3])}: {_name(ruleset, segs[1])}"
    if segs[0] == "resource" and len(segs) == 3:
        return f"доход «{_name(ruleset, segs[1])}»"
    if segs[0] == "province" and len(segs) == 3:
        return f"доход «{_name(ruleset, segs[2])}» провинции"
    if segs[0] == "spy" and len(segs) == 4 and segs[1] == "mission":
        m = ruleset.missions.get(segs[2])
        what = {"difficulty": "сложность", "max_rounds": "наибольший срок"}.get(segs[3], segs[3])
        return f"{what} миссии «{m.name if m else segs[2].split(':')[-1]}»"
    if segs[0] == "spy" and len(segs) == 4 and segs[1] in ("shade", "ward"):
        who = "Тени" if segs[1] == "shade" else "Заставы"
        what = {"cost": "цена", "upkeep": "содержание"}.get(segs[2], segs[2])
        return f"{what} {who} ({_name(ruleset, segs[3])})"
    if segs[0] == "spy" and len(segs) == 3:
        group = {"success": "успех задания", "detect": "поимка Тени", "attribution": "узнать хозяина",
                 "feint_expose": "разоблачение ложной записи"}.get(segs[1], segs[1])
        return f"{group}: {segs[2]}"
    if segs[0] == "disposition" and len(segs) == 3:
        d = ruleset.dispositions.get(segs[1])
        what = {"cost_mul": "цена", "loot_mul": "добыча"}.get(segs[2], segs[2])
        return f"{what} судьбы «{d.name if d else segs[1].split(':')[-1]}»"
    if segs[0] == "tech" and len(segs) == 4:
        return f"цена технологий ветки «{_name(ruleset, segs[2])}»"
    if segs[0] == "tech" and len(segs) == 3:
        return f"цена технологии «{_name(ruleset, segs[1])}»"
    return path


def _number(op: str, value: float, path: str) -> str:
    spec = registry.stat_spec(path)
    percent = spec is not None and spec.kind is float and spec.hi <= 1.0 and op != "mul"
    if op == "mul":
        return f"×{value:g}"
    if op == "set":
        return f"= {value:.0%}" if percent else f"= {value:g}"
    return f"{value:+.0%}" if percent else f"{value:+g}"


def describe_effect(ruleset: Ruleset, e: Mapping[str, Any]) -> str:
    kind = e.get("type", e.get("effect"))
    if kind == "modify":
        scope = {"province": " (в своей провинции)", "owner": " (владельцу)"}.get(e.get("scope"), "")
        return f"{stat_text(ruleset, e['target'])} {_number(e['op'], e['value'], e['target'])}{scope}"
    if kind == "income":
        return f"доход {_name(ruleset, e['resource'])} {e['amount']:+d}/ход"
    if kind == "add_resource":
        return f"{_name(ruleset, e['resource'])} {e['amount']:+d} на старте"
    if kind == "grant_units":
        return f"{_name(ruleset, e['unit'])}×{e['count']} в столице на старте"
    if kind == "grant_building":
        return f"{_name(ruleset, e['building'])} в столице с начала"
    if kind == "grant_tech":
        return f"технология «{_name(ruleset, e['tech'])}» с начала"
    if kind == "unlock":
        return f"открывает {KIND_LABELS.get(e['kind'], e['kind'])} «{_name(ruleset, e['id'])}»"
    if kind == "set_flag":
        return f"флаг {e['flag']}"
    return str(kind)


def _label(text: str) -> str:
    return text.strip().lower().rstrip(".")


FUZZY_MIN = 3        # shortest partial name `find` tries
FUZZY_MAX = 5        # more partial matches than this are not an answer


def _entity_names(fid: str, entity: Any) -> set[str]:
    names = {fid.lower(), fid.split(":", 1)[1].lower(), _label(entity.name)}
    names.add(_label(getattr(entity, "name_en", "") or "") or fid.lower())
    names.add(_label(getattr(entity, "short", "") or "") or fid.lower())
    return names


def find(ruleset: Ruleset, ref: Any) -> list[tuple[str, str]]:
    """[(kind, full id)] matching a full id, a short id, a name (ru/en) or a short label, any case. No exact match:
    names (or ids) that start with `ref`, else contain it (at least FUZZY_MIN letters, at most FUZZY_MAX hits), so
    «Литания» finds «Литания Ржавого Престола»."""
    if not isinstance(ref, str) or not ref.strip():
        return []
    label = _label(ref)
    entries = [(kind, fid, _entity_names(fid, entity)) for kind in KINDS
               for fid, entity in getattr(ruleset, _TABLES[kind]).items()]
    out = [(kind, fid) for kind, fid, names in entries if label in names]
    if out or len(label) < FUZZY_MIN:
        return out
    for match in (lambda n: n.startswith(label), lambda n: label in n):
        hits = [(kind, fid) for kind, fid, names in entries if any(match(n) for n in names)]
        if hits:
            return hits if len(hits) <= FUZZY_MAX else []
    return []


def _amounts(ruleset: Ruleset, amounts: Mapping[str, int]) -> str:
    return ", ".join(f"{n} {_name(ruleset, r)}" for r, n in amounts.items()) or "0"


def describe(ruleset: Ruleset, kind: str, fid: str) -> dict[str, Any]:
    """Full description of one entity (public content of the game): fields plus Russian `text`."""
    entity = getattr(ruleset, _TABLES[kind])[fid]
    out: dict[str, Any] = {"kind": kind, "id": fid, "name": entity.name}
    lines = [f"{KIND_LABELS[kind].capitalize()} «{entity.name}» ({fid})"]
    if kind == "unit":
        st = ruleset.stat
        abilities = {n: {p: st(f"unit.{fid}.ability.{n}.{p}") for p in registry.ABILITIES[n]}
                     for n in registry.ABILITIES if st(f"unit.{fid}.ability.{n}.on")}
        out.update(attack=int(st(f"unit.{fid}.attack")), defense=int(st(f"unit.{fid}.defense")),
                   cost=ruleset.unit_cost(fid), upkeep=ruleset.unit_upkeep(fid), counters=list(entity.counters),
                   move=int(st(f"unit.{fid}.move")), requires_tech=list(entity.requires_tech),
                   requires_building=list(entity.requires_building), factions=list(entity.factions),
                   abilities=abilities)
        lines.append(f"атака {out['attack']}, оборона {out['defense']}, цена {_amounts(ruleset, out['cost'])}, "
                     f"содержание {_amounts(ruleset, out['upkeep'])}/ход, ход по своей земле {out['move']}")
        if entity.counters:
            lines.append("бьёт: " + ", ".join(_name(ruleset, c) for c in entity.counters))
        for n, params in abilities.items():
            lines.append("способность: " + ABILITY_TEXT[n].format(**params))
        if entity.requires_tech or entity.requires_building:
            lines.append("требует: " + ", ".join(_name(ruleset, x) for x in
                                                  (*entity.requires_tech, *entity.requires_building)))
        if entity.factions:
            lines.append("только фракции: " + ", ".join(_name(ruleset, f) for f in entity.factions))
    elif kind == "building":
        out.update(cost=ruleset.building_cost(fid), build_turns=entity.build_turns, income=dict(entity.income),
                   recruit=entity.recruit, requires_tech=list(entity.requires_tech), factions=list(entity.factions),
                   effects=[describe_effect(ruleset, e) for e in (*entity.province_mods, *entity.owner_mods)])
        lines.append(f"цена {_amounts(ruleset, out['cost'])}, строится {entity.build_turns} ход(а)"
                     + (f", доход +{_amounts(ruleset, entity.income)}" if entity.income else "")
                     + (", позволяет найм" if entity.recruit else ""))
        if entity.payment == "parts":
            # core 0.7.0 (decision 27): the shares of the base price (a faction's own price splits the same way)
            n = ruleset.building_shares(fid)
            shares = [split_cost(out["cost"], n, i) for i in range(n)]
            out.update(payment="parts", shares=shares)
            lines.append(f"оплата долями: {' / '.join(_amounts(ruleset, s) for s in shares)} — первая с приказом, "
                         "следующие на шаге строек каждого хода; нет доли — стройка стоит, но не пропадает")
        if entity.score and ruleset.weight("works"):
            pts = int(ruleset.stat(f"building.{fid}.score"))
            out.update(score=pts, max_per_owner=entity.max_per_owner, allowed_types=list(entity.allowed_types))
            lines.append(f"великая стройка: +{pts} очк., пока готова в твоей полноценной провинции; начало и "
                         "готовность видят все")
        if entity.max_per_owner:
            lines.append(f"не больше {entity.max_per_owner} у одного игрока (готовые и строящиеся)")
        if entity.allowed_types:
            lines.append("только в провинциях типа: " + ", ".join(_name(ruleset, t) for t in entity.allowed_types))
        lines += [f"- {x}" for x in out["effects"]]
        if entity.requires_tech:
            lines.append("требует: " + ", ".join(_name(ruleset, x) for x in entity.requires_tech))
        if entity.factions:
            lines.append("только фракции: " + ", ".join(_name(ruleset, f) for f in entity.factions))
    elif kind == "tech":
        out.update(name_en=entity.name_en, branch=entity.branch, tier=entity.tier, cost=ruleset.tech_cost(fid),
                   requires=list(entity.requires), requires_any=list(entity.requires_any),
                   requires_tier=entity.requires_tier, factions=list(entity.factions),
                   effects=[describe_effect(ruleset, e) for e in entity.effects], unlocks=list(entity.unlocks))
        lines.append(f"ветка «{_name(ruleset, entity.branch)}», ярус {entity.tier}, цена {out['cost']}")
        if entity.requires:
            lines.append("нужны: " + ", ".join(_name(ruleset, x) for x in entity.requires))
        if entity.requires_any:
            lines.append("нужна одна из: " + ", ".join(_name(ruleset, x) for x in entity.requires_any))
        if entity.requires_tier:
            rt = entity.requires_tier
            lines.append(f"нужно {rt['count']} технологии яруса {rt['tier']}"
                         + (" своей ветки" if rt.get("same_branch", True) else ""))
        lines += [f"- {x}" for x in out["effects"]]
        if entity.unlocks:
            lines.append("открывает: " + ", ".join(_name(ruleset, x) for x in entity.unlocks))
    elif kind == "artifact":
        out.update(name_en=entity.name_en, power=entity.power, cost=entity.cost, factions=list(entity.factions),
                   exclusive_with=list(entity.exclusive_with),
                   effects=[describe_effect(ruleset, e) for e in entity.effects])
        lines.append(f"сила {entity.power}, стоимость {entity.cost} очк.")
        lines += [f"- {x}" for x in out["effects"]]
        if entity.factions:
            lines.append("только фракции: " + ", ".join(_name(ruleset, f) for f in entity.factions))
        if entity.exclusive_with:
            lines.append("несовместим с: " + ", ".join(_name(ruleset, x) for x in entity.exclusive_with))
    elif kind == "faction":
        out.update(bonuses=[describe_effect(ruleset, e) for e in entity.bonuses],
                   penalties=[describe_effect(ruleset, e) for e in entity.penalties],
                   start_resources=dict(entity.start_resources), start_units=dict(entity.start_units),
                   start_buildings=list(entity.start_buildings),
                   start_effects=[describe_effect(ruleset, e) for e in entity.start_effects],
                   unique_units=sorted(u for u, d in ruleset.units.items() if fid in d.factions),
                   unique_buildings=sorted(b for b, d in ruleset.buildings.items() if fid in d.factions),
                   unique_techs=sorted(t for t, d in ruleset.techs.items() if fid in d.factions))
        if entity.description:
            lines.append(entity.description)
        lines += [f"+ {x}" for x in out["bonuses"]] + [f"− {x}" for x in out["penalties"]]
        start = [_amounts(ruleset, entity.start_resources)]
        start += [f"{_name(ruleset, u)}×{n}" for u, n in entity.start_units.items()]
        start += [_name(ruleset, b) for b in entity.start_buildings] + out["start_effects"]
        lines.append("старт: " + ", ".join(start))
        unique = out["unique_units"] + out["unique_buildings"] + out["unique_techs"]
        if unique:
            lines.append("уникальное: " + ", ".join(_name(ruleset, x) for x in unique))
    elif kind == "branch":
        out.update(name_en=entity.name_en, order=entity.order, description=entity.description, color=entity.color,
                   techs=sorted(t for t, d in ruleset.techs.items() if d.branch == fid))
        if entity.description:
            lines.append(entity.description)
        lines.append("технологии: " + ", ".join(_name(ruleset, t) for t in out["techs"]))
    elif kind == "objective":
        from .scoring import objective_text     # scoring imports this module's neighbours
        out.update(kind_id=entity.kind, params=dict(entity.params), points=entity.points,
                   weighted=entity.points * ruleset.weight("objectives"))
        tier = DIFFICULTY_TEXT.get(entity.difficulty or "")
        if tier:
            out["difficulty"] = entity.difficulty
        lines.append(f"{objective_text(ruleset, None, {'id': fid})}; +{out['weighted']} очк."
                     + (f" ({tier})" if tier else "") + " к итогу, если "
                     "выполнена в конце (проверка при game_over). Кому какая цель — тайна до конца партии")
    elif kind == "discovery":
        from .discoveries import choice_text, effects_text     # discoveries imports content and combat
        out.update(find_kind=entity.kind, rarity=entity.rarity, weight=entity.weight, power=entity.power,
                   risk=entity.risk, power_small=list(entity.power_small), ev=dict(entity.ev),
                   default_choice=entity.default_choice)
        lines.append(f"{DISCOVERY_KIND_TEXT[entity.kind]}, {RARITY_TEXT.get(entity.rarity, entity.rarity)}; сила "
                     f"{entity.power:+.2f} P (5 штурм.), одиночке {entity.power_small[0]:+.2f} P"
                     + (f", риск {entity.risk:.2f}" if entity.kind == "mixed" else ""))
        if entity.effects:
            lines.append("при входе армии: " + effects_text(ruleset, entity.effects))
        for c in entity.choices:
            lines.append(f"- {c['label']}: {choice_text(ruleset, entity, c)}"
                         + (" — по умолчанию" if c["id"] == entity.default_choice else ""))
        if entity.description:
            lines.append(entity.description)
        lines.append("где лежит — тайна; Наблюдение раскрывает, не запуская")
    elif kind == "resource":
        out.update(role=entity.role)
        lines.append(f"роль: {entity.role}")
    elif kind == "province_type":
        out.update(income=dict(entity.income), defense_mul=entity.defense_mul)
        lines.append(f"доход {_amounts(ruleset, entity.income)}, оборона ×{entity.defense_mul:g}")
    lore = getattr(entity, "lore", "") or ""
    if lore:
        out["lore"] = lore
        lines.append(lore)
    out["text"] = "\n".join(lines)
    return out
