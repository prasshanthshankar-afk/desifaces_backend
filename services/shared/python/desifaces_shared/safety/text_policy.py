from __future__ import annotations

import re
from typing import Iterable

from .contracts import SafetyDecision
from .policy import category_finding, pass_finding
from .contracts import decision_from_findings


_TEXT_RULES: tuple[dict[str, str], ...] = (
    {
        "pattern": r"\b(child|kid|minor|underage)\b.{0,80}\b(nude|naked|sexual|sex|porn|pornographic|explicit|erotic|seductive|lingerie|bikini|revealing)\b",
        "category": "minors",
        "reason": "sexualized or unsafe content involving minors",
        "action": "Remove references to minors in sexual, nude, revealing, or unsafe contexts.",
    },
    {
        "pattern": r"\b(?:[0-9]|1[0-7])\s*[- ]?\s*(?:years?|yrs?)\s*[- ]?\s*old\b.{0,80}\b(nude|naked|sexual|sex|porn|pornographic|explicit|erotic|seductive|lingerie|bikini|revealing)\b",
        "category": "minors",
        "reason": "sexualized or unsafe content involving minors",
        "action": "Remove sexual, nude, lingerie, revealing, or unsafe treatment of anyone under 18.",
    },
    {
        "pattern": r"\b(?:teen|teenage|teenager)\b.{0,80}\b(nude|naked|sexual|sex|porn|pornographic|explicit|erotic|seductive|lingerie|revealing)\b",
        "category": "minors",
        "reason": "sexualized or unsafe content involving young people",
        "action": "Use an explicitly adult subject and remove sexual or unsafe young-person context.",
    },
    {
        "pattern": r"\b(nude|naked|porn|pornographic|nsfw|obscene|explicit sexual|sexual act|sex act|erotic sex|exposed genitals)\b",
        "category": "sexual",
        "reason": "nudity, pornography, explicit sexual content, or exposed intimate body parts",
        "action": "Keep subjects fully clothed and use a non-explicit context.",
    },
    {
        "pattern": r"\b(rape|sexual assault|molest|child abuse|child exploitation|underage sex)\b",
        "category": "abuse",
        "reason": "sexual violence, exploitation, or abuse",
        "action": "Remove abuse or exploitation references and use a safe non-sexual context.",
    },
    {
        "pattern": r"\b(?:gun|guns|firearm|firearms|rifle|rifles|pistol|pistols|handgun|handguns|shotgun|shotguns|revolver|revolvers|machine[- ]?gun|machine[- ]?guns|assault[- ]?rifle|assault[- ]?rifles|sniper[- ]?rifle|sniper[- ]?rifles|weapon|weapons|ammunition|ammo)\b",
        "category": "weapons",
        "reason": "guns, firearms, weapons, ammunition, or armed weapon-focused imagery",
        "action": "Remove guns, firearms, weapons, ammunition, and armed poses.",
    },
    {
        "pattern": r"\b(?:gore|gory|blood|bloody|bloodbath|bleeding|blood[- ]?splatter|blood[- ]?stain(?:ed|s)?|open wounds?|dismember|decapitat(?:e|ed|ion)|mutilat(?:e|ed|ion))\b",
        "category": "violence",
        "reason": "blood, gore, graphic injury, mutilation, or extreme violence",
        "action": "Use a safe non-graphic scene without blood, gore, wounds, or graphic bodily harm.",
    },
    {
        "pattern": r"\b(terrorist|terrorism|extremist|extremism|isis|isil|daesh|al[ -]?qaeda)\b.{0,100}\b(propaganda|recruit|recruitment|join|support|praise|glorify|fund|donate|manifesto|attack instructions?)\b",
        "category": "terrorism",
        "reason": "terrorist or extremist propaganda, recruitment, praise, material support, or attack facilitation",
        "action": "Remove promotional, recruitment, support, praise, or attack-facilitation content involving terrorist or extremist activity.",
    },
    {
        "pattern": r"\b(make|manufacture|cook|synthesize|traffic|sell)\b.{0,60}\b(cocaine|heroin|meth|fentanyl|illegal drugs?)\b",
        "category": "illegal_drugs",
        "reason": "instructions or facilitation for illegal drug activity",
        "action": "Remove drug-making, selling, trafficking, or usage instructions.",
    },
)


def evaluate_text_policy(text: str, *, source: str = "text") -> SafetyDecision:
    normalized = str(text or "").strip()
    findings = []
    for rule in _TEXT_RULES:
        if re.search(rule["pattern"], normalized, flags=re.IGNORECASE | re.DOTALL):
            findings.append(
                category_finding(
                    rule["category"],
                    source=source,
                    reason=rule["reason"],
                    required_action=rule["action"],
                )
            )
    if findings:
        return decision_from_findings(findings)
    return decision_from_findings([pass_finding(source=source)])


def evaluate_texts_policy(values: Iterable[str], *, source: str = "text") -> SafetyDecision:
    findings = []
    for value in values:
        decision = evaluate_text_policy(value, source=source)
        findings.extend(item for item in decision.findings if not item.code == "CONTENT_SAFETY_PASS")
    if findings:
        return decision_from_findings(findings)
    return decision_from_findings([pass_finding(source=source)])
