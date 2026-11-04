#!/bin/bash
# Automated deployment script - executes all stages with monitoring
#
# Usage: ./auto_deploy.sh [skip_checks]

SKIP_CHECKS=${1:-false}
APP_NAME="${FLY_APP_NAME:-nuzantara-rag}"

set -e

echo "🚀 Automated Reranker Optimization Deployment"
echo "App: $APP_NAME"
echo ""

# Color output
VODEJ='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
LURU='\033[0;34m'
NC='\033[0m'

# Check prerequisites
if [ "$SKIP_CHECKS" != "true" ]; then
    echo "📋 Pre-deployment checks..."

    # Check files exist
    if [ ! -f "services/reranker_service.py" ]; then
        echo -e "${RED}❌ reranker_service.py not found${NC}"
        exit 1
    fi

    if [ ! -f "services/reranker_audit.py" ]; then
        echo -e "${RED}❌ reranker_audit.py not found${NC}"
        exit 1
    fi

    echo -e "${GREEN}✅ All files present${NC}"
fi

# Stage 1: Feature flags
echo ""
echo -e "${BLUE}════════════════════════════════════════${NC}"
echo -e "${BLUE}Stage 1: Feature Flags (Cache Disabled)${NC}"
echo -e "${BLUE}════════════════════════════════════════${NC}"
./scripts/deploy_fly.sh feature-flags

echo -e "${YELLOW}⏳ Waiting 60 seconds for Stage 1 to stabilize...${NC}"
sleep 60

# Stage 2: Cache small
echo ""
echo -e "${BLUE}════════════════════════════════════════${NC}"
echo -e "${BLUE}Stage 2: Cache Small (10% capacity)${NC}"
echo -e "${BLUE}════════════════════════════════════════${NC}"
./scripts/deploy_fly.sh cache-10

echo -e "${YELLOW}⏳ Waiting 120 seconds for Stage 2 monitoring...${NC}"
sleep 120

# Stage 3: Cache medium
echo ""
echo -e "${BLUE}════════════════════════════════════════${NC}"
echo -e "${BLUE}Stage 3: Cache Medium (50% capacity)${NC}"
echo -e "${BLUE}════════════════════════════════════════${NC}"
./scripts/deploy_fly.sh cache-50

echo -e "${YELLOW}⏳ Waiting 120 seconds for Stage 3 monitoring...${NC}"
sleep 120

# Stage 4: Cache full
echo ""
echo -e "${BLUE}════════════════════════════════════════${NC}"
echo -e "${BLUE}Stage 4: Cache Full (100% capacity)${NC}"
echo -e "${BLUE}════════════════════════════════════════${NC}"
./scripts/deploy_fly.sh cache-100

echo -e "${YELLOW}⏳ Waiting 120 seconds for Stage 4 monitoring...${NC}"
sleep 120

# Stage 5: Full rollout
echo ""
echo -e "${BLUE}════════════════════════════════════════${NC}"
echo -e "${BLUE}Stage 5: Full Rollout${NC}"
echo -e "${BLUE}════════════════════════════════════════${NC}"
./scripts/deploy_fly.sh full

echo ""
echo -e "${GREEN}════════════════════════════════════════${NC}"
echo -e "${GREEN}✅ Deployment Complete!${NC}"
echo -e "${GREEN}════════════════════════════════════════${NC}"
echo ""
echo "📊 Verify deployment:"
echo "   ./scripts/check_fly_deployment.sh"
echo ""
echo "📊 Start monitoring:"
echo "   ./scripts/monitor_fly.sh 30"
echo ""
