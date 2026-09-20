#ifndef GOLDSCOUT_TAKE_PROFIT_POLICY_MQH
#define GOLDSCOUT_TAKE_PROFIT_POLICY_MQH

// PAPER-only take-profit policy. The caller supplies closed-bar structural
// evidence; this module never changes a signal, stop, volume or risk budget.
enum GoldScoutTPConfidence
{
   GOLDSCOUT_TP_CONFIDENCE_NONE   = 0,
   GOLDSCOUT_TP_CONFIDENCE_LOW    = 1,
   GOLDSCOUT_TP_CONFIDENCE_MEDIUM = 2,
   GOLDSCOUT_TP_CONFIDENCE_HIGH   = 3
};

struct GoldScoutTPZone
{
   GoldScoutPivotType    type;
   double                price;
   int                   evidenceCount;
   int                   weightTotal;
   int                   touches;
   datetime              firstTime;
   datetime              lastTime;
   bool                  hasH1Pivot;
   bool                  hasH4Pivot;
   bool                  hasM15Pivot;
   bool                  hasEqualLevel;
   bool                  hasRecentH1Extreme;
   bool                  hasConsolidation;
   bool                  hasBreakoutLevel;
   GoldScoutTPConfidence confidence;
};

struct GoldScoutTakeProfitDecision
{
   bool                  valid;
   double                currentTP;
   double                currentRR;
   double                v2TP;
   double                v2RR;
   double                selectedTP;
   double                selectedRR;
   double                structureLevel;
   GoldScoutTPConfidence structureConfidence;
   string                tradeClass;
   string                selectedMode;
   string                selectionReason;
};

const double GOLDSCOUT_TP_V2_CHILL_R=0.75;
const double GOLDSCOUT_TP_V2_GOD_BASELINE_R=1.25;
const double GOLDSCOUT_TP_V2_BUFFER_ATR=0.20;
const double GOLDSCOUT_TP_ZONE_MERGE_ATR=0.15;
const double GOLDSCOUT_TP_EQUAL_LEVEL_ATR=0.20;

bool GSTP_ValidPositive(const double value)
{
   return MathIsValidNumber(value) && value>0.0;
}

void GSTP_ClearZones(GoldScoutTPZone &zones[])
{
   ArrayResize(zones,0);
}

void GSTP_ClearDecision(GoldScoutTakeProfitDecision &decision)
{
   decision.valid=false;
   decision.currentTP=0.0;
   decision.currentRR=0.0;
   decision.v2TP=0.0;
   decision.v2RR=0.0;
   decision.selectedTP=0.0;
   decision.selectedRR=0.0;
   decision.structureLevel=0.0;
   decision.structureConfidence=GOLDSCOUT_TP_CONFIDENCE_NONE;
   decision.tradeClass="NONE";
   decision.selectedMode="CURRENT";
   decision.selectionReason="NOT_EVALUATED";
}

string GSTP_ConfidenceName(const GoldScoutTPConfidence confidence)
{
   if(confidence==GOLDSCOUT_TP_CONFIDENCE_HIGH) return "HIGH";
   if(confidence==GOLDSCOUT_TP_CONFIDENCE_MEDIUM) return "MEDIUM";
   if(confidence==GOLDSCOUT_TP_CONFIDENCE_LOW) return "LOW";
   return "NONE";
}

double GSTP_AlignPrice(const double price,const double tickSize,const int digits,
                       const bool roundUp)
{
   if(!GSTP_ValidPositive(price) || !GSTP_ValidPositive(tickSize) || digits<0)
      return 0.0;
   double ticks=price/tickSize;
   double aligned=(roundUp?MathCeil(ticks-1e-9):MathFloor(ticks+1e-9))*tickSize;
   return NormalizeDouble(aligned,digits);
}

double GSTP_RewardRisk(const int direction,const double entry,const double stop,
                       const double target)
{
   double riskDistance=MathAbs(entry-stop);
   if((direction!=1 && direction!=-1) || !GSTP_ValidPositive(entry) ||
      !GSTP_ValidPositive(stop) || !GSTP_ValidPositive(target) ||
      riskDistance<=0.0)
      return 0.0;
   double rewardDistance=direction>0 ? target-entry : entry-target;
   return rewardDistance>0.0 ? rewardDistance/riskDistance : 0.0;
}

bool GSTP_PaperV2Enabled(const bool requested,const bool liveTrading)
{
   return requested && !liveTrading;
}

GoldScoutTPConfidence GSTP_ClassifyZone(const GoldScoutTPZone &zone)
{
   if(zone.hasH4Pivot || zone.hasEqualLevel || zone.touches>=2 ||
      (zone.hasH1Pivot && zone.hasM15Pivot))
      return GOLDSCOUT_TP_CONFIDENCE_HIGH;
   if(zone.hasH1Pivot || zone.hasRecentH1Extreme || zone.hasConsolidation ||
      zone.hasBreakoutLevel)
      return GOLDSCOUT_TP_CONFIDENCE_MEDIUM;
   return GOLDSCOUT_TP_CONFIDENCE_LOW;
}

bool GSTP_AddEvidence(GoldScoutTPZone &zones[],const GoldScoutPivotType type,
                      const double price,const datetime time,const int weight,
                      const bool h1Pivot,const bool h4Pivot,const bool m15Pivot,
                      const bool recentH1Extreme,const bool consolidation,
                      const bool breakoutLevel,const double atr,const double point)
{
   if((type!=GOLDSCOUT_PIVOT_HIGH && type!=GOLDSCOUT_PIVOT_LOW) ||
      !GSTP_ValidPositive(price) || time<=0 || weight<1 ||
      !GSTP_ValidPositive(atr) || !GSTP_ValidPositive(point))
      return false;

   double mergeTolerance=MathMax(point,atr*GOLDSCOUT_TP_ZONE_MERGE_ATR);
   int match=-1;
   double nearest=DBL_MAX;
   for(int i=0;i<ArraySize(zones);i++)
   {
      if(zones[i].type!=type) continue;
      double distance=MathAbs(zones[i].price-price);
      if(distance<=mergeTolerance+1e-9 && distance<nearest)
      {
         nearest=distance;
         match=i;
      }
   }

   if(match<0)
   {
      int size=ArraySize(zones);
      if(ArrayResize(zones,size+1)!=size+1) return false;
      match=size;
      zones[match].type=type;
      zones[match].price=price;
      zones[match].evidenceCount=0;
      zones[match].weightTotal=0;
      zones[match].touches=0;
      zones[match].firstTime=time;
      zones[match].lastTime=time;
      zones[match].hasH1Pivot=false;
      zones[match].hasH4Pivot=false;
      zones[match].hasM15Pivot=false;
      zones[match].hasEqualLevel=false;
      zones[match].hasRecentH1Extreme=false;
      zones[match].hasConsolidation=false;
      zones[match].hasBreakoutLevel=false;
      zones[match].confidence=GOLDSCOUT_TP_CONFIDENCE_LOW;
   }
   else if(time!=zones[match].firstTime && time!=zones[match].lastTime &&
           MathAbs(zones[match].price-price)<=MathMax(point,atr*GOLDSCOUT_TP_EQUAL_LEVEL_ATR)+1e-9)
   {
      zones[match].hasEqualLevel=true;
   }

   int priorWeight=zones[match].weightTotal;
   zones[match].price=(zones[match].price*priorWeight+price*weight)/(priorWeight+weight);
   zones[match].weightTotal=priorWeight+weight;
   zones[match].evidenceCount++;
   if(zones[match].touches==0 || (time!=zones[match].firstTime && time!=zones[match].lastTime))
      zones[match].touches++;
   if(time<zones[match].firstTime) zones[match].firstTime=time;
   if(time>zones[match].lastTime) zones[match].lastTime=time;
   zones[match].hasH1Pivot=zones[match].hasH1Pivot || h1Pivot;
   zones[match].hasH4Pivot=zones[match].hasH4Pivot || h4Pivot;
   zones[match].hasM15Pivot=zones[match].hasM15Pivot || m15Pivot;
   zones[match].hasRecentH1Extreme=zones[match].hasRecentH1Extreme || recentH1Extreme;
   zones[match].hasConsolidation=zones[match].hasConsolidation || consolidation;
   zones[match].hasBreakoutLevel=zones[match].hasBreakoutLevel || breakoutLevel;
   zones[match].confidence=GSTP_ClassifyZone(zones[match]);
   return true;
}

bool GSTP_FirstRelevantObstacle(const GoldScoutTPZone &zones[],const int direction,
                                const double entry,const double baseline,
                                double &level,GoldScoutTPConfidence &confidence)
{
   level=0.0;
   confidence=GOLDSCOUT_TP_CONFIDENCE_NONE;
   if((direction!=1 && direction!=-1) || !GSTP_ValidPositive(entry) ||
      !GSTP_ValidPositive(baseline))
      return false;
   GoldScoutPivotType wanted=direction>0?GOLDSCOUT_PIVOT_HIGH:GOLDSCOUT_PIVOT_LOW;
   double nearest=DBL_MAX;
   for(int i=0;i<ArraySize(zones);i++)
   {
      if(zones[i].type!=wanted ||
         zones[i].confidence<GOLDSCOUT_TP_CONFIDENCE_MEDIUM)
         continue;
      bool inside=direction>0
         ? zones[i].price>entry && zones[i].price<=baseline+1e-9
         : zones[i].price<entry && zones[i].price>=baseline-1e-9;
      double distance=MathAbs(zones[i].price-entry);
      if(inside && distance<nearest)
      {
         nearest=distance;
         level=zones[i].price;
         confidence=zones[i].confidence;
      }
   }
   return GSTP_ValidPositive(level);
}

bool GSTP_BuildDecision(const int direction,const string tradeClass,
                        const double entry,const double stop,const double atr,
                        const double tickSize,const int digits,const double currentTP,
                        const GoldScoutTPZone &zones[],const bool useV2,
                        const bool liveTrading,GoldScoutTakeProfitDecision &decision)
{
   GSTP_ClearDecision(decision);
   if((direction!=1 && direction!=-1) ||
      (tradeClass!="CHILL" && tradeClass!="GOD") ||
      !GSTP_ValidPositive(entry) || !GSTP_ValidPositive(stop) ||
      !GSTP_ValidPositive(atr) || !GSTP_ValidPositive(tickSize) ||
      !GSTP_ValidPositive(currentTP))
      return false;

   double riskDistance=MathAbs(entry-stop);
   if(riskDistance<=0.0) return false;
   double baselineR=tradeClass=="CHILL"
      ? GOLDSCOUT_TP_V2_CHILL_R : GOLDSCOUT_TP_V2_GOD_BASELINE_R;
   double rawBaseline=direction>0
      ? entry+baselineR*riskDistance : entry-baselineR*riskDistance;
   // Match the historical fixed-R candidate: round outward on the broker grid.
   double v2TP=GSTP_AlignPrice(rawBaseline,tickSize,digits,direction>0);
   if(!GSTP_ValidPositive(v2TP)) return false;

   decision.currentTP=currentTP;
   decision.currentRR=GSTP_RewardRisk(direction,entry,stop,currentTP);
   decision.tradeClass=tradeClass;
   decision.selectionReason=tradeClass=="CHILL"
      ? "CHILL_FIXED_0.75R" : "GOD_BASELINE_1.25R_NO_OBSTACLE";

   if(tradeClass=="GOD")
   {
      double obstacle=0.0;
      GoldScoutTPConfidence obstacleConfidence=GOLDSCOUT_TP_CONFIDENCE_NONE;
      if(GSTP_FirstRelevantObstacle(zones,direction,entry,v2TP,
                                    obstacle,obstacleConfidence))
      {
         double rawStructural=direction>0
            ? obstacle-GOLDSCOUT_TP_V2_BUFFER_ATR*atr
            : obstacle+GOLDSCOUT_TP_V2_BUFFER_ATR*atr;
         // A structural target is rounded toward entry and never pushed farther
         // merely to manufacture a reward multiple.
         double structural=GSTP_AlignPrice(rawStructural,tickSize,digits,direction<0);
         double structuralRR=GSTP_RewardRisk(direction,entry,stop,structural);
         double baselineRR=GSTP_RewardRisk(direction,entry,stop,v2TP);
         if(structuralRR>0.0 && structuralRR+1e-9<baselineRR)
         {
            v2TP=structural;
            decision.structureLevel=obstacle;
            decision.structureConfidence=obstacleConfidence;
            decision.selectionReason="GOD_STRUCTURAL_OBSTACLE_BUFFER_0.20_ATR_LOW_REWARD_ALLOWED";
         }
      }
   }

   decision.v2TP=v2TP;
   decision.v2RR=GSTP_RewardRisk(direction,entry,stop,v2TP);
   if(decision.currentRR<=0.0 || decision.v2RR<=0.0) return false;
   bool applyV2=GSTP_PaperV2Enabled(useV2,liveTrading);
   decision.selectedTP=applyV2?decision.v2TP:decision.currentTP;
   decision.selectedRR=applyV2?decision.v2RR:decision.currentRR;
   decision.selectedMode=applyV2?"V2":"CURRENT";
   if(useV2 && liveTrading)
      decision.selectionReason="LIVE_GUARD_CURRENT";
   decision.valid=true;
   return true;
}

#endif
