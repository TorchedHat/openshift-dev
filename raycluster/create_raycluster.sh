#!/bin/bash

while true; do
  read -p "Enter namespace: " NAMESPACE
  read -e -i "${ANTHROPIC_VERTEX_PROJECT_ID:-}" -p "Enter vertex project ID: " PROJECT_ID

  echo ""
  echo "You entered:"
  echo "  Namespace:         $NAMESPACE"
  echo "  Vertex Project ID: $PROJECT_ID"
  echo ""
  read -p "Is this correct? (y/n): " CONFIRM

  if [[ "$CONFIRM" =~ ^[Yy]$ ]]; then
    break
  fi

  echo "Let's try again."
  echo ""
done

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
YAML_FILE="$SCRIPT_DIR/raycluster-3w.yml"

yq eval '
  (.. | select(tag == "!!str")) |= sub("<username>", "'"$NAMESPACE"'") |
  (.spec.headGroupSpec.template.spec.containers[] |
    select(.name == "ray-head").env[] |
    select(.name == "ANTHROPIC_VERTEX_PROJECT_ID")).value = "'"$PROJECT_ID"'"
' "$YAML_FILE" | oc apply -n "$NAMESPACE" -f -
