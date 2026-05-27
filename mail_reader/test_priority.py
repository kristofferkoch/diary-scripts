"""Heuristic-floor scoring — pure function, no DB needed."""
from __future__ import annotations

from . import priority


def test_astrid_is_high():
    assert priority.score("Astrid Kristine Hansen <astrid.hansen@gmail.com>") == priority.HIGH


def test_contractor_domain_is_high():
    assert priority.score("Pat Olsen <olsen@eksempel-el.no>") == priority.HIGH
    assert priority.score("post@exampleconcrete.no") == priority.HIGH


def test_examplefund_person_is_high_but_role_is_not():
    assert priority.score("Ole Berg <Ole.Berg@examplefund.no>") == priority.HIGH
    # markedskommentar@ is a newsletter — no pattern matches, falls to DEFAULT.
    # (Not NOISE because the noise patterns don't catch role addresses.)
    assert priority.score("Examplefund AS <markedskommentar@examplefund.no>") == priority.DEFAULT


def test_noreply_at_important_domain_still_noise():
    """If a domain we care about sends from a `noreply@` address, that's
    almost certainly an automated notification — treat it as noise."""
    assert priority.score("noreply@examplefund.no") == priority.NOISE


def test_linkedin_is_noise():
    assert priority.score("LinkedIn <updates-noreply@linkedin.com>") == priority.NOISE


def test_storytel_is_noise():
    assert priority.score("Storytel <hello@email.storytel.com>") == priority.NOISE


def test_filter_aktuelt_is_noise():
    assert priority.score("Filter Aktuelt <aktuelt@filtermedia.no>") == priority.NOISE


def test_proton_newsletter_is_noise():
    assert priority.score("Proton <no-reply@news.proton.me>") == priority.NOISE


def test_finn_noreply_is_noise():
    assert priority.score("noreply <noreply@finn.no>") == priority.NOISE


def test_google_notification_is_noise():
    assert priority.score("Google <no-reply@accounts.google.com>") == priority.NOISE


def test_amazonses_subdomain_is_noise():
    """ESP mass-send infrastructure — every sender via this is automated."""
    assert priority.score("foo@eu-central-1.amazonses.com") == priority.NOISE


def test_unknown_personal_is_default():
    """Random gmail person we haven't seeded isn't elevated; it's also
    not noise — just default-priority."""
    assert priority.score("random.person@gmail.com") == priority.DEFAULT


def test_recruiter_via_linkedin_is_noise():
    """Even though Kristin's pitch is content-relevant, LinkedIn's
    InMail-relay address routes through linkedin.com infrastructure —
    the heuristic-floor can't see body. This is the conservative
    failure mode: real important content from a noise-coded sender
    will need tier-1 LLM refinement to lift back up. Test pins
    today's behaviour."""
    assert priority.score("Kristin Halvorsen <inmail-hit-reply@linkedin.com>") == priority.NOISE


def test_empty_or_missing_is_default():
    assert priority.score("") == priority.DEFAULT
    assert priority.score(None) == priority.DEFAULT


def test_case_insensitivity():
    """Patterns are case-insensitive on both sides."""
    assert priority.score("ASTRID.HANSEN@GMAIL.COM") == priority.HIGH
    assert priority.score("Ole.BERG@EXAMPLEFUND.NO") == priority.HIGH
    assert priority.score("NoReply@Linkedin.com") == priority.NOISE
