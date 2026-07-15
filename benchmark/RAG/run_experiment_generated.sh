#!/bin/bash
set -euo pipefail

DATASET="${1:-hotpotqa}"
BUILD_COUNT="${2:-1}"
STEP="${3:-gen+eval}"
OV_CONF="${4:-ov.conf}"

case "$DATASET" in
    financebench)
        CONFIG_DIR="config/financebench"
        CONFIG_PREFIX="financebench"
        MIXED_DATASET="FinanceBench"
        ;;
    hotpotqa)
        CONFIG_DIR="config/hotpotqa"
        CONFIG_PREFIX="hotpotqa"
        MIXED_DATASET="HotpotQA"
        ;;
    legalbench_contractnli)
        CONFIG_DIR="config/legalbench/contractnli"
        CONFIG_PREFIX="legalbench_contractnli"
        MIXED_DATASET="LegalBench_ContractNLI"
        ;;
    legalbench_cuad)
        CONFIG_DIR="config/legalbench/cuad"
        CONFIG_PREFIX="legalbench_cuad"
        MIXED_DATASET="LegalBench_CUAD"
        ;;
    legalbench_maud)
        CONFIG_DIR="config/legalbench/maud"
        CONFIG_PREFIX="legalbench_maud"
        MIXED_DATASET="LegalBench_MAUD"
        ;;
    qasper)
        CONFIG_DIR="config/qasper"
        CONFIG_PREFIX="qasper"
        MIXED_DATASET="Qasper"
        ;;
    syllabusqa)
        CONFIG_DIR="config/syllabusqa"
        CONFIG_PREFIX="syllabusqa"
        MIXED_DATASET="SyllabusQA"
        ;;
    versionrag)
        CONFIG_DIR="config/versionrag"
        CONFIG_PREFIX="versionrag"
        MIXED_DATASET="VersionRAG"
        ;;
    *)
        echo "Unsupported generated-question dataset: $DATASET" >&2
        echo "Supported: financebench, hotpotqa, legalbench_contractnli, legalbench_cuad, legalbench_maud, qasper, syllabusqa, versionrag" >&2
        exit 2
        ;;
esac

if [[ ! "$BUILD_COUNT" =~ ^[1-9][0-9]*$ ]]; then
    echo "BUILD_COUNT must be a positive integer: $BUILD_COUNT" >&2
    exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

GENERATED_CONFIG="${CONFIG_DIR}/${CONFIG_PREFIX}_generated_questions_build_links_review.yaml"
MIXED_PATH="generated_questions/${MIXED_DATASET}/mixed_questions.jsonl"

if [[ ! -f "$GENERATED_CONFIG" ]]; then
    echo "Generated build-link config not found: $GENERATED_CONFIG" >&2
    exit 2
fi
if [[ ! -f "$MIXED_PATH" ]]; then
    echo "Mixed questions not found: ${SCRIPT_DIR}/${MIXED_PATH}" >&2
    exit 2
fi

echo "Validating mixed questions: $MIXED_PATH"
python scripts/validate_mixed_questions.py "$MIXED_PATH"

echo "=========================================="
echo " OpenViking Generated Questions Experiment"
echo " Dataset: $DATASET"
echo " Mixed questions: $MIXED_PATH"
echo " Build count: $BUILD_COUNT"
echo " Step: $STEP"
echo " OV Config: $OV_CONF"
echo "=========================================="

echo ""
echo "[1/8] Non-bot baseline: ${CONFIG_PREFIX}_config.yaml"
echo "------------------------------------------"
python run.py --config "${CONFIG_DIR}/${CONFIG_PREFIX}_config.yaml" --step "$STEP" --ov-conf "$OV_CONF"

echo ""
echo "[2/8] Bot baseline: ${CONFIG_PREFIX}_bot_config.yaml"
echo "------------------------------------------"
python run.py --config "${CONFIG_DIR}/${CONFIG_PREFIX}_bot_config.yaml" --step gen+eval --ov-conf "$OV_CONF"

echo ""
echo "[3/8] Generated mixed build_links_review x${BUILD_COUNT}: $(basename "$GENERATED_CONFIG")"
echo "------------------------------------------"
for i in $(seq 1 "$BUILD_COUNT"); do
    echo "  >> Build round $i / $BUILD_COUNT"
    python run.py --config "$GENERATED_CONFIG" --step gen --ov-conf "$OV_CONF"
done

echo ""
echo "[4/8] Non-bot relations_review: ${CONFIG_PREFIX}_config_relations_review.yaml"
echo "------------------------------------------"
python run.py --config "${CONFIG_DIR}/${CONFIG_PREFIX}_config_relations_review.yaml" --step gen+eval --ov-conf "$OV_CONF"

echo ""
echo "[5/8] Bot relations_review: ${CONFIG_PREFIX}_bot_config_relations_review.yaml"
echo "------------------------------------------"
python run.py --config "${CONFIG_DIR}/${CONFIG_PREFIX}_bot_config_relations_review.yaml" --step gen+eval --ov-conf "$OV_CONF"

echo ""
echo "[6/8] OV fallback bot: ${CONFIG_PREFIX}_ov_fallback_bot_config.yaml"
echo "------------------------------------------"
python run.py --config "${CONFIG_DIR}/${CONFIG_PREFIX}_ov_fallback_bot_config.yaml" --step gen+eval --ov-conf "$OV_CONF"

echo ""
echo "[7/8] OV fallback bot relations: ${CONFIG_PREFIX}_ov_fallback_bot_relations_config.yaml"
echo "------------------------------------------"
python run.py --config "${CONFIG_DIR}/${CONFIG_PREFIX}_ov_fallback_bot_relations_config.yaml" --step gen+eval --ov-conf "$OV_CONF"

echo ""
echo "[8/8] OV fallback bot relations naive rule: ${CONFIG_PREFIX}_ov_fallback_bot_relations_naive_rule_config.yaml"
echo "------------------------------------------"
python run.py --config "${CONFIG_DIR}/${CONFIG_PREFIX}_ov_fallback_bot_relations_naive_rule_config.yaml" --step gen+eval --ov-conf "$OV_CONF"

echo ""
echo "=========================================="
echo " Experiment complete!"
echo "=========================================="
