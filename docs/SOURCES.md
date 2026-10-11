# Data Sources Reference Guide

This document records the official endpoints, authentication requirements, rate limits, licences, and change-feed endpoints for all external data sources integrated into `norway-profiler`.

---

## 1. Enhetsregisteret (Brønnøysundregistrene)

- **Base URL**: `https://data.brreg.no/enhetsregisteret/api`
- **Endpoints by Org Number**:
  - **Main Units (Enheter)**: `https://data.brreg.no/enhetsregisteret/api/enheter/{orgnr}`
  - **Sub-Units (Underenheter)**: `https://data.brreg.no/enhetsregisteret/api/underenheter/{orgnr}`
  - **Roles (Roller)**: `https://data.brreg.no/enhetsregisteret/api/enheter/{orgnr}/roller`
- **Update / Change-Feed Endpoints**:
  - `https://data.brreg.no/enhetsregisteret/api/oppdateringer/enheter` (query parameters: `oppdateringsid`, `dato`, `size`, `page`)
  - `https://data.brreg.no/enhetsregisteret/api/oppdateringer/underenheter` (query parameters: `oppdateringsid`, `dato`, `size`, `page`)
- **Authentication**: None (Free public open API)
- **Rate Limits**: UNVERIFIED (Standard HTTP exponential backoff recommended)
- **Licence / Terms of Reuse**: Norsk lisens for åpne data (NLOD) / Creative Commons CC BY 4.0

---

## 2. Regnskapsregisteret (Brønnøysundregistrene)

- **Base URL**: `https://data.brreg.no/regnskapsregisteret/regnskap`
- **Endpoint by Org Number**:
  - `https://data.brreg.no/regnskapsregisteret/regnskap/{orgnr}`
- **Update / Change-Feed Endpoint**: UNVERIFIED
- **Authentication**: None
- **Rate Limits**: UNVERIFIED
- **Licence / Terms of Reuse**: Norsk lisens for åpne data (NLOD)

---

## 3. Kunngjøringer (Brønnøysundregistrene)

- **Status**: NOT USED: no verified public API (web search and paid XML subscription only)

---

## 4. Doffin (Norwegian Public Procurement Portal)

- **Status**: NOT USED: official Public API v2 needs a key from the Doffin developer portal and supplier-orgnr filtering is UNVERIFIED.
- **Base URL**: `https://api.doffin.no/public/v2`
- **Authentication**: Requires subscription key (`Ocp-Apim-Subscription-Key` via `DOFFIN_API_KEY` env var) from Doffin developer portal
- **Licence / Terms of Reuse**: Norwegian Public Procurement Open Data Terms

---

## 5. GLEIF (Global Legal Entity Identifier Foundation)

- **Base URL**: `https://api.gleif.org/api/v1`
- **Endpoint by Org Number**:
  - `https://api.gleif.org/api/v1/lei-records?filter[entity.registeredAs]={orgnr}`
- **Update / Change-Feed Endpoint**:
  - Daily Golden Copy pubfiles: `https://api.gleif.org/api/v1/golden-copies/pubfiles`
- **Authentication**: None
- **Rate Limits**: 1000 requests per minute (UNVERIFIED exact per-IP threshold)
- **Licence / Terms of Reuse**: CC0 1.0 Universal (Public Domain)

---

## 6. Wikidata

- **Base URL**: `https://query.wikidata.org/sparql`
- **Endpoint by Org Number**:
  - SPARQL query filtering Property `wdt:P2333` (Norwegian organisation number):
    `SELECT ?item WHERE { ?item wdt:P2333 "{orgnr}" }`
- **Update / Change-Feed Endpoint**:
  - Recent Changes API: `https://www.wikidata.org/w/api.php?action=query&list=recentchanges`
- **Authentication**: None (Custom `User-Agent` HTTP header required)
- **Rate Limits**: Max 60 requests per minute per User-Agent on SPARQL Query Service
- **Licence / Terms of Reuse**: CC0 1.0 Universal
