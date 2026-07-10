#!/bin/bash
set -e

DATASET="${1:-hotpotqa}"
BUILD_COUNT="${2:-1}"
STEP="${3:-gen+eval}"
OV_CONF="${4:-ov.conf}"

CONFIG_DIR="config/${DATASET}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "=========================================="
echo " OpenViking Relations Experiment"
echo " Dataset: $DATASET"
echo " Build count: $BUILD_COUNT"
echo " Step: $STEP"
echo " OV Config: $OV_CONF"
echo "=========================================="

echo ""
echo "[1/8] Non-bot baseline: ${DATASET}_config.yaml"
echo "------------------------------------------"
python run.py --config "${CONFIG_DIR}/${DATASET}_config.yaml" --step "$STEP" --ov-conf "$OV_CONF"

echo ""
echo "[2/8] Bot baseline: ${DATASET}_bot_config.yaml"
echo "------------------------------------------"
python run.py --config "${CONFIG_DIR}/${DATASET}_bot_config.yaml" --step gen+eval --ov-conf "$OV_CONF"

echo ""
echo "[3/8] Bot build_links_review x${BUILD_COUNT}: ${DATASET}_bot_config_build_links_review.yaml"
echo "------------------------------------------"
for i in $(seq 1 "$BUILD_COUNT"); do
    echo "  >> Build round $i / $BUILD_COUNT"
    python run.py --config "${CONFIG_DIR}/${DATASET}_bot_config_build_links_review.yaml" --step gen+eval --ov-conf "$OV_CONF"
done

echo ""
echo "[4/8] Non-bot relations_review: ${DATASET}_config_relations_review.yaml"
echo "------------------------------------------"
python run.py --config "${CONFIG_DIR}/${DATASET}_config_relations_review.yaml" --step gen+eval --ov-conf "$OV_CONF"

echo ""
echo "[5/8] Bot relations_review: ${DATASET}_bot_config_relations_review.yaml"
echo "------------------------------------------"
python run.py --config "${CONFIG_DIR}/${DATASET}_bot_config_relations_review.yaml" --step gen+eval --ov-conf "$OV_CONF"

echo ""
echo "[6/8] OV fallback bot: ${DATASET}_ov_fallback_bot_config.yaml"
echo "------------------------------------------"
python run.py --config "${CONFIG_DIR}/${DATASET}_ov_fallback_bot_config.yaml" --step gen+eval --ov-conf "$OV_CONF"

echo ""
echo "[7/8] OV fallback bot relations: ${DATASET}_ov_fallback_bot_relations_config.yaml"
echo "------------------------------------------"
python run.py --config "${CONFIG_DIR}/${DATASET}_ov_fallback_bot_relations_config.yaml" --step gen+eval --ov-conf "$OV_CONF"

echo ""
echo "[8/8] OV fallback bot relations naive rule: ${DATASET}_ov_fallback_bot_relations_naive_rule_config.yaml"
echo "------------------------------------------"
python run.py --config "${CONFIG_DIR}/${DATASET}_ov_fallback_bot_relations_naive_rule_config.yaml" --step gen+eval --ov-conf "$OV_CONF"

echo ""
echo "=========================================="
echo " Experiment complete!"
echo "=========================================="
