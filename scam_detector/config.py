"""
config.py
---------
Single source of truth for all tunable knobs:
thresholds, weights, and feature flags.

Import anywhere via:
    from scam_detector.config import cfg
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ScoringWeights(BaseModel):
    """Relative weights applied when combining individual risk signals."""

    text_weight: float = Field(default=0.25, ge=0.0, le=1.0)
    company_weight: float = Field(default=0.20, ge=0.0, le=1.0)
    url_weight: float = Field(default=0.20, ge=0.0, le=1.0)
    stipend_weight: float = Field(default=0.15, ge=0.0, le=1.0)
    temporal_weight: float = Field(default=0.10, ge=0.0, le=1.0)
    structural_weight: float = Field(default=0.10, ge=0.0, le=1.0)


class BlendWeights(BaseModel):
    """
    Weights for blending rules-engine score with anomaly-model score and optional supervised model.

    Default: 60% rules / 40% anomaly / 0% supervised. When supervised model is active, weights are normalized.

    reputation_weight was raised from 0.10 to 0.25: validated end-to-end
    that a single confirmed_scam feedback label immediately raises
    reputation_score to 1.0 for any other listing from that company, but at
    0.10 that only moved an otherwise-clean listing's score by ~9 points
    (e.g. 0.0 -> 9.09) - nowhere near enough to meaningfully flag a repeat
    offender on its own. At 0.25 the same case reaches ~20 points, and it
    meaningfully compounds with any other signal on the listing rather than
    being diluted into irrelevance.
    """

    rules_weight: float = Field(default=0.60, ge=0.0, le=1.0)
    anomaly_weight: float = Field(default=0.40, ge=0.0, le=1.0)
    supervised_weight: float = Field(default=0.0, ge=0.0, le=1.0)
    reputation_weight: float = Field(default=0.25, ge=0.0, le=1.0)




class RuleWeights(BaseModel):
    """
    Per-rule weights for the deterministic rules engine.

    Each value is the risk contribution [0, 1] added when the rule fires.
    Tune here without touching rule logic.

    Rationale behind defaults
    -------------------------
    hard_disqualifying_signals : 0.95  — near-automatic escalation to human
                                         review; only 1 other signal can override
    guaranteed_outcome_claim    : 0.85  — "100% guaranteed placement", "no
                                         interview" — legitimate internships
                                         never make these claims
    cross_company_duplicate     : 0.80  — same script / multiple shells is the
                                         strongest single fraud indicator
    typosquat_domain            : 0.70  — off-platform mismatch is strong
    extreme_stipend_outlier     : 0.45  — both directions are suspicious but
                                         context matters; moderate weight
    mass_openings_vague_role    : 0.40  — combined heuristic; moderate
    stipend_perk_contradiction  : 0.35  — data quality issue, not certain fraud
    unverifiable_company        : 0.10  — scraper noise, very low weight;
                                         reduces CONFIDENCE, not fraud score
    """

    hard_disqualifying_signals: float = Field(default=0.95, ge=0.0, le=1.0)
    guaranteed_outcome_claim: float = Field(default=0.85, ge=0.0, le=1.0)
    external_form_handoff: float = Field(default=0.60, ge=0.0, le=1.0)
    training_program_disguised_as_internship: float = Field(default=0.65, ge=0.0, le=1.0)
    zero_shot_semantic_scam_signal: float = Field(default=0.55, ge=0.0, le=1.0)
    young_domain_age: float = Field(default=0.55, ge=0.0, le=1.0)
    similar_to_confirmed_scam: float = Field(default=0.70, ge=0.0, le=1.0)
    upfront_fee_and_pay_to_work: float = Field(default=0.90, ge=0.0, le=1.0)
    cross_company_duplicate: float = Field(default=0.80, ge=0.0, le=1.0)
    typosquat_domain: float = Field(default=0.70, ge=0.0, le=1.0)
    shared_infrastructure: float = Field(default=0.65, ge=0.0, le=1.0)
    ngo_fundraising_stipend_network: float = Field(default=0.65, ge=0.0, le=1.0)
    suspicious_recruiter_contact: float = Field(default=0.50, ge=0.0, le=1.0)
    extreme_stipend_outlier: float = Field(default=0.45, ge=0.0, le=1.0)
    urgency_psychological_pressure: float = Field(default=0.45, ge=0.0, le=1.0)
    mass_openings_vague_role: float = Field(default=0.40, ge=0.0, le=1.0)
    stipend_perk_contradiction: float = Field(default=0.35, ge=0.0, le=1.0)
    unverifiable_company: float = Field(default=0.10, ge=0.0, le=1.0)


class RuleThresholds(BaseModel):
    """
    Per-rule numeric thresholds, configurable without touching rule logic.
    """

    # ExtremeStipendOutlierRule: |z-score| must exceed this to trigger
    stipend_zscore_threshold: float = Field(default=3.0, ge=0.0)

    # TyposquatDomainRule: domain_company_similarity below this on off-platform link
    typosquat_similarity_threshold: float = Field(default=0.35, ge=0.0, le=1.0)

    # MassOpeningsVagueRoleRule: openings z-score above this AND genericity above this
    mass_openings_zscore_threshold: float = Field(default=2.0, ge=0.0)
    mass_openings_genericity_threshold: float = Field(default=0.65, ge=0.0, le=1.0)

    # UrgencyAndPsychologicalPressureRule thresholds
    urgency_score_threshold: float = Field(default=0.65, ge=0.0, le=1.0)
    emotional_manipulation_threshold: float = Field(default=0.50, ge=0.0, le=1.0)

    # NgoFundraisingStipendNetworkRule: minimum distinct companies sharing
    # the exact same lump-sum stipend amount to flag a coordinated network
    ngo_stipend_min_distinct_companies: int = Field(default=3, ge=2)

    # YoungDomainAgeRule: domain must be younger than this to trigger.
    # Only evaluated for genuine off-platform employer domains — platform/
    # ATS links are gated out before the WHOIS lookup even happens (see
    # extract_company_url_features in company_features.py).
    young_domain_age_days_threshold: int = Field(default=90, ge=0)

    # SimilarToConfirmedScamRule: minimum SBERT cosine similarity to a
    # human-confirmed scam posting (from the feedback loop) to trigger.
    scam_corpus_similarity_threshold: float = Field(default=0.85, ge=0.0, le=1.0)

    # Minimum peer group size required to compute stable z-scores
    min_peer_group_size: int = Field(
        default=8,
        ge=2,
        description="Minimum comparable peer records required to compute z-scores for stipend and openings",
    )


class Thresholds(BaseModel):
    """
    Legacy 0–1 decision boundaries (kept for older callers).

    Prefer :class:`DecisionThresholds` for the Phase 5 scam_score (0–100) path.
    """

    auto_approve_below: float = Field(default=0.25, ge=0.0, le=1.0)
    pending_review_below: float = Field(default=0.55, ge=0.0, le=1.0)
    # scores >= pending_review_below are classified as high-risk / auto-rejected


class DecisionThresholds(BaseModel):
    """
    Explicit Phase 5 decision thresholds on the 0–100 ``scam_score`` scale.

    Defaults: score < 30 → clear, 30–70 → review, ≥ 70 → block.
    Easy to tune per deployment without changing scoring code.
    """

    clear_below: float = Field(
        default=30.0,
        ge=0.0,
        le=100.0,
        description="scam_score strictly below this → clear (unless confidence forces review)",
    )
    block_at_or_above: float = Field(
        default=70.0,
        ge=0.0,
        le=100.0,
        description="scam_score at or above this → block",
    )


class ConfidenceConfig(BaseModel):
    """Confidence banding and low-confidence decision override."""

    low_below: float = Field(
        default=0.40,
        ge=0.0,
        le=1.0,
        description=(
            "Confidence below this forces decision to at least 'review' — "
            "a low-confidence 'clear' is misleading given Phase 1 unreliable fields."
        ),
    )
    medium_below: float = Field(
        default=0.70,
        ge=0.0,
        le=1.0,
        description="Confidence in [low_below, medium_below) → medium; else high",
    )
    enable_source_conditioning: bool = Field(
        default=True,
        description="Adjust sparse fields penalty relative to per-source baseline completeness.",
    )
    source_baseline_path: str = Field(
        default="data/source_baselines.json",
        description="Path to save/load per-source baseline statistics.",
    )
    global_completeness_target: float = Field(
        default=0.66,
        description="Target reference completeness value to scale against.",
    )


class FeatureFlags(BaseModel):
    """Toggle individual feature groups on/off without changing pipeline code."""

    enable_text_features: bool = True
    enable_company_features: bool = True
    enable_url_features: bool = True
    enable_stipend_features: bool = True
    enable_temporal_features: bool = True
    enable_structural_features: bool = True
    enable_ml_risk_engine: bool = False  # off until a trained model is present
    enable_shap_anomaly_explanations: bool = True  # use SHAP TreeExplainer for anomaly explanations


class EmbeddingConfig(BaseModel):
    """
    Shared sentence-embedding settings for Prompt 2 (text features) and
    Prompt 5 (duplicate index).  Both modules must read ``sbert_model_name``
    from here — never hardcode the model string in two places.
    """

    sbert_model_name: str = Field(
        default="all-MiniLM-L6-v2",
        description=(
            "sentence-transformers model id used by title_summary_alignment, "
            "boilerplate_similarity, and DuplicateIndex.  Swap to e.g. "
            "all-mpnet-base-v2 for higher quality at higher cost."
        ),
    )


class SupervisedModelConfig(BaseModel):
    """Configuration for supervised scam classifier."""

    model_path: str = Field(
        default="scam_detector/models/supervised_model.joblib",
        description="Path to serialized supervised model artifact",
    )
    min_training_samples: int = Field(
        default=500,
        ge=1,
        description="Minimum labeled records required to train supervised model",
    )


class CalibrationConfig(BaseModel):
    """Configuration for isotonic score calibrator."""

    model_path: str = Field(
        default="scam_detector/models/calibration_model.joblib",
        description="Path to serialized calibration model artifact",
    )
    min_calibration_samples: int = Field(
        default=50,
        ge=1,
        description="Minimum labeled records required to fit calibration model",
    )


class ReputationConfig(BaseModel):
    """Configuration for company reputation features and store."""

    store_path: str = Field(
        default="scam_detector/reputation.jsonl",
        description="Path to serialized company reputation store",
    )


class FeedbackConfig(BaseModel):
    """Configuration for the human-review feedback store (feedback.py)."""

    store_path: str = Field(
        default="scam_detector/feedback.jsonl",
        description="Path to the append-only JSONL review-feedback store",
    )


class ZeroShotConfig(BaseModel):
    """
    Configuration for the optional zero-shot semantic scam classifier.

    ⚠️  KNOWN UNRELIABLE — DO NOT ENABLE WITHOUT RE-VALIDATION. ⚠️
    Initial validation against 8 hand-picked examples (3 known real scams +
    3 paraphrases + 2 clean) looked strong and correctly generalized past
    regex phrasing. But a follow-up test against a RANDOM 250-record sample
    of real data told a different story: 49/250 (19.6%) flagged — far above
    the true scam rate in this corpus. Almost every false positive lands on
    the SAME label, "sells a paid training or certification course
    disguised as a job" — including completely ordinary postings from real
    companies like Airbus and established local firms. The model cannot
    distinguish "this internship teaches you skills" (true of nearly every
    legitimate internship) from "this internship IS a fake training
    product" (the actual red flag) — the label is too close to what
    defines an internship in general, not a deviation from it.

    Lesson: 8 examples is not enough to validate a probabilistic signal.
    Before ever enabling this, either drop/rework the training-course label
    or find a materially higher confidence threshold, and re-validate
    against a large random sample — not hand-picked cases — same as any
    other rule in this engine.

    Off by default: both for the false-positive rate above AND because
    each call costs ~0.5-0.8s of CPU inference, too slow for a large batch
    scoring run.
    """

    enabled: bool = Field(
        default=False,
        description="Off by default: ~20% false-positive rate on real data (see class docstring) AND ~0.5-0.8s/record CPU latency",
    )
    model_name: str = Field(
        default="MoritzLaurer/deberta-v3-xsmall-zeroshot-v1.1-all-33",
        description="Compact (~146MB) NLI model tuned for zero-shot classification",
    )
    confidence_threshold: float = Field(
        default=0.60,
        ge=0.0,
        le=1.0,
        description="Minimum top-label confidence to treat as a triggered signal",
    )


class AnomalyConfig(BaseModel):
    """Configuration for unsupervised anomaly model and explanations."""

    enable_shap: bool = Field(
        default=True,
        description="When True, use SHAP TreeExplainer for anomaly explanations; falls back to z-score approximation when False or unavailable.",
    )
    n_estimators: int = Field(default=100, ge=10)
    contamination: float | str = Field(default="auto")


class Config(BaseModel):
    """Top-level config object — instantiate once and share."""

    weights: ScoringWeights = Field(default_factory=ScoringWeights)
    blend_weights: BlendWeights = Field(default_factory=BlendWeights)
    rule_weights: RuleWeights = Field(default_factory=RuleWeights)
    rule_thresholds: RuleThresholds = Field(default_factory=RuleThresholds)
    thresholds: Thresholds = Field(default_factory=Thresholds)
    decision_thresholds: DecisionThresholds = Field(default_factory=DecisionThresholds)
    confidence: ConfidenceConfig = Field(default_factory=ConfidenceConfig)
    embeddings: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    supervised: SupervisedModelConfig = Field(default_factory=SupervisedModelConfig)
    calibration: CalibrationConfig = Field(default_factory=CalibrationConfig)
    reputation: ReputationConfig = Field(default_factory=ReputationConfig)
    feedback: FeedbackConfig = Field(default_factory=FeedbackConfig)
    anomaly: AnomalyConfig = Field(default_factory=AnomalyConfig)
    zero_shot: ZeroShotConfig = Field(default_factory=ZeroShotConfig)
    flags: FeatureFlags = Field(default_factory=FeatureFlags)

    # When Prompt 6 rule 1 (hard_disqualifying_signals) fires, force this
    # decision regardless of the blended score.  Some deployments prefer
    # human review even on the clearest cases rather than full auto-block.
    hard_disqualifying_decision: Literal["block", "review"] = "block"


# Module-level singleton — override fields as needed in tests or via env vars.
cfg = Config()

