#!/bin/bash
set -e

DATASET="${1:-hotpotqa}"
BUILD_COUNT="${2:-1}"
STEP="${3:-all}"

CONFIG_DIR="config/${DATASET}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "=========================================="
echo " OpenViking Relations Experiment"
echo " Dataset: $DATASET"
echo " Build count: $BUILD_COUNT"
echo " Step: $STEP"
echo "=========================================="

echo ""
echo "[1/5] Non-bot baseline: ${DATASET}_config.yaml"
echo "------------------------------------------"
python run.py --config "${CONFIG_DIR}/${DATASET}_config.yaml" --step "$STEP"

echo ""
echo "[2/5] Bot baseline: ${DATASET}_bot_config.yaml"
echo "------------------------------------------"
python run.py --config "${CONFIG_DIR}/${DATASET}_bot_config.yaml" --step "$STEP"

echo ""
echo "[3/5] Bot build_links_review x${BUILD_COUNT}: ${DATASET}_bot_config_build_links_review.yaml"
echo "------------------------------------------"
for i in $(seq 1 "$BUILD_COUNT"); do
    echo "  >> Build round $i / $BUILD_COUNT"
    python run.py --config "${CONFIG_DIR}/${DATASET}_bot_config_build_links_review.yaml" --step "$STEP"
done

echo ""
echo "[4/5] Non-bot relations_review: ${DATASET}_config_relations_review.yaml"
echo "------------------------------------------"
python run.py --config "${CONFIG_DIR}/${DATASET}_config_relations_review.yaml" --step "$STEP"

echo ""
echo "[5/5] Bot relations_review: ${DATASET}_bot_config_relations_review.yaml"
echo "------------------------------------------"
python run.py --config "${CONFIG_DIR}/${DATASET}_bot_config_relations_review.yaml" --step "$STEP"

echo ""
echo "=========================================="
echo " Experiment complete!"
echo "=========================================="
