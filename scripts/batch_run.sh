#!/usr/bin/env bash
# scripts/batch_run.sh
# Executes day-sized batch runs of 100-150 Norwegian company profiles,
# saving each run_report with the date.

set -e

BATCH_SIZE=100
SEED_FILE="data/seed.txt"
OUT_DIR="out"
HARD_CASES=""

while [[ $# -gt 0 ]]; do
  case $1 in
    --hard-cases)
      HARD_CASES="--hard-cases"
      shift
      ;;
    --batch-size)
      BATCH_SIZE="$2"
      shift 2
      ;;
    --seed-file)
      SEED_FILE="$2"
      shift 2
      ;;
    --out-dir)
      OUT_DIR="$2"
      shift 2
      ;;
    *)
      echo "Unknown option: $1"
      exit 1
      ;;
  esac
done

echo "================================================="
echo " Starting Norwegian Profiler Batch Run"
echo " Batch Size : ${BATCH_SIZE}"
echo " Seed File  : ${SEED_FILE}"
echo " Hard Cases : ${HARD_CASES:-No}"
echo " Output Dir : ${OUT_DIR}"
echo "================================================="

mkdir -p data "${OUT_DIR}"

if [[ ! -f "${SEED_FILE}" ]] || [[ -n "${HARD_CASES}" ]]; then
  echo "[+] Generating seed file ${SEED_FILE}..."
  python -m profiler seed --count 1000 --out "${SEED_FILE}" --summary-out "data/seed_summary.json" ${HARD_CASES}
fi

TODAY=$(date +%Y-%m-%d)
TMP_BATCH_FILE="data/batch_tmp.txt"

echo "[+] Preparing batch from ${SEED_FILE}..."
grep -v '^#' "${SEED_FILE}" | grep -v '^[[:space:]]*$' | head -n "${BATCH_SIZE}" > "${TMP_BATCH_FILE}"

NUM_ORGS=$(wc -l < "${TMP_BATCH_FILE}" | tr -d ' ')
echo "[+] Running batch of ${NUM_ORGS} companies..."

python -m profiler run --input "${TMP_BATCH_FILE}" --out "${OUT_DIR}" --resume

REPORT_FILE="${OUT_DIR}/run_report.json"
DATE_REPORT_FILE="${OUT_DIR}/run_report_${TODAY}.json"

if [[ -f "${REPORT_FILE}" ]]; then
  cp "${REPORT_FILE}" "${DATE_REPORT_FILE}"
  echo "[+] Saved daily run report to ${DATE_REPORT_FILE}"
else
  echo "[!] Warning: ${REPORT_FILE} not found."
fi

rm -f "${TMP_BATCH_FILE}"

echo "[+] Batch run completed successfully for ${TODAY}!"
