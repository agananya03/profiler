# Confidence Score Calculation Formula

This document details the mathematical formula used by `norway-profiler` to compute confidence scores for reconciled profile facts.

---

## 1. Overview

The confidence score $C \in [0.0, 1.0]$ represents the probabilistic certainty of an attributed profile fact. It combines four objective factors:

1. **Source Tier Weight ($W_{\text{tier}}$)**: The authority and trustworthiness of the data provider.
2. **Match Basis Strength ($W_{\text{match}}$)**: The cryptographic or explicit identifier link connecting the data to the entity.
3. **Fact Freshness ($W_{\text{age}}$)**: Exponential age decay based on retrieval date.
4. **Cross-Source Agreement Bonus ($W_{\text{agree}}$)**: Boost applied when multiple independent sources corroborate the same value.

---

## 2. Mathematical Formula

$$\text{Confidence} = \min\left(1.0, \, \text{round}\left(W_{\text{tier}} \times W_{\text{match}} \times W_{\text{age}} + W_{\text{agree}}, \, 4\right)\right)$$

---

## 3. Factor Parameters & Weights

### A. Source Tier Weight ($W_{\text{tier}}$)
| Tier | Description | Sources | Weight ($W_{\text{tier}}$) |
|---|---|---|---|
| **Tier 1** | Official Government Registry | Brønnøysundregistrene (Enheter, Regnskap, Kunngjøringer, Roller) | **1.00** |
| **Tier 2** | Verified Official Register | GLEIF (LEI), Doffin (Public Procurement) | **0.90** |
| **Tier 3** | Deterministic Structured Data | Website Parsing (Schema.org JSON-LD, Meta, Socials), Wikidata | **0.80** |
| **Tier 4** | Verbatim LLM Verified Text | Verbatim LLM Extracted Claims (`llm.py`) | **0.70** |

---

### B. Match Basis Strength ($W_{\text{match}}$)
| Match Basis | Description | Weight ($W_{\text{match}}$) |
|---|---|---|
| `orgnr_exact`, `orgnr_on_page` | Direct 9-digit orgnr match in official record or page text | **1.00** |
| `wikidata_property_p2333` | Exact Wikidata P2333 property match | **1.00** |
| `supplier_orgnum` | Exact Doffin supplier organisation number match | **1.00** |
| `domain_match` | Homepage matching registry official domain fact | **0.90** |
| `redirected_domain` | Redirected to different domain | **0.70** |

---

### C. Fact Age Decay ($W_{\text{age}}$)
Age is calculated relative to retrieval timestamp:

$$W_{\text{age}} = \exp\left(-\frac{\text{age\_days}}{365.0}\right)$$

*For facts retrieved within 30 days, $W_{\text{age}} \approx 1.00$.*

---

### D. Cross-Source Agreement Bonus ($W_{\text{agree}}$)
- **+0.05** bonus if 2 or more independent data sources emit the exact same normalized value for the field.
- **+0.00** if only a single source provides the value.

---

## 4. Example Calculation

Suppose **NORWEGIAN TECH LABS AS** has its legal name confirmed by both BRREG (`Enhetsregisteret`, Tier 1) and GLEIF (Tier 2):

- $W_{\text{tier}} = 1.00$ (Tier 1 Registry)
- $W_{\text{match}} = 1.00$ (`orgnr_exact`)
- $W_{\text{age}} = 1.00$ (Retrieved today)
- $W_{\text{agree}} = +0.05$ (GLEIF corroborates legal name)

$$\text{Confidence} = \min(1.0, 1.00 \times 1.00 \times 1.00 + 0.05) = 1.00$$
