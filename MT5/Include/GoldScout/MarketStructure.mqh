#ifndef GOLDSCOUT_MARKET_STRUCTURE_MQH
#define GOLDSCOUT_MARKET_STRUCTURE_MQH

// Confirmed-pivot foundation and deterministic market-structure classifier.
enum GoldScoutPivotType
{
   GOLDSCOUT_PIVOT_LOW  = -1,
   GOLDSCOUT_PIVOT_NONE = 0,
   GOLDSCOUT_PIVOT_HIGH = 1
};

struct GoldScoutPivot
{
   GoldScoutPivotType type;
   double             price;
   int                shift;
   datetime           time;
   double             atr;
   bool               confirmed;
};

struct GoldScoutPivotConfig
{
   int    leftBars;
   int    rightBars;
   int    minBarsBetween;
   double minProminenceAtr;
   double toleranceAtr;
};

enum GoldScoutStructureState
{
   GOLDSCOUT_STRUCTURE_BEARISH     = -1,
   GOLDSCOUT_STRUCTURE_INSUFFICIENT = 0,
   GOLDSCOUT_STRUCTURE_BULLISH      = 1,
   GOLDSCOUT_STRUCTURE_NEUTRAL      = 2
};

struct GoldScoutStructureClassification
{
   GoldScoutStructureState state;
   bool                     sufficient;
   bool                     hh;
   bool                     hl;
   bool                     lh;
   bool                     ll;
   int                      alternatingPivotCount;
};

struct GoldScoutM15TimingEvidence
{
   bool                     available;
   datetime                 closedBarTime;
   GoldScoutStructureState  structure;
   bool                     haveLastSwingHigh;
   bool                     haveLastSwingLow;
   double                   lastSwingHigh;
   double                   lastSwingLow;
   int                      breakoutDirection;
   int                      recoveryDirection;
   int                      momentumDirection;
   int                      longAdjustment;
   int                      shortAdjustment;
};

enum GoldScoutPatternType
{
   GOLDSCOUT_PATTERN_M    = -1,
   GOLDSCOUT_PATTERN_NONE = 0,
   GOLDSCOUT_PATTERN_W    = 1
};

enum GoldScoutPatternState
{
   GOLDSCOUT_PATTERN_STATE_NONE        = 0,
   GOLDSCOUT_PATTERN_STATE_CANDIDATE   = 1,
   GOLDSCOUT_PATTERN_STATE_CONFIRMED   = 2,
   GOLDSCOUT_PATTERN_STATE_INVALIDATED = 3,
   GOLDSCOUT_PATTERN_STATE_EXPIRED     = 4
};

struct GoldScoutPatternConfig
{
   double extremeToleranceAtr;
   double minDepthAtr;
   int    minPivotBars;
   int    maxPivotBars;
   int    maxConfirmationBars;
   double breakoutBufferAtr;
   double invalidationAtr;
   double minLegBalance;
   bool   useVolumeQuality;
   int    volumeLookback;
   double volumeMultiplier;
   bool   useMomentumQuality;
   double momentumBodyAtr;
};

struct GoldScoutPatternDiagnostic
{
   bool                      detected;
   GoldScoutPatternType      type;
   GoldScoutPatternState     state;
   GoldScoutPivot            first;
   GoldScoutPivot            neckline;
   GoldScoutPivot            second;
   datetime                  eventTime;
   int                       firstLegBars;
   int                       secondLegBars;
   int                       barsAfterSecond;
   double                    referenceAtr;
   double                    extremeTolerance;
   double                    depth;
   double                    breakoutStrengthAtr;
   bool                      volumeConfirmed;
   bool                      momentumConfirmed;
   double                    quality;
   string                    identity;
};

enum GoldScoutContinuationPatternType
{
   GOLDSCOUT_CONTINUATION_NONE         = 0,
   GOLDSCOUT_CONTINUATION_BULL_FLAG    = 1,
   GOLDSCOUT_CONTINUATION_BEAR_FLAG    = 2,
   GOLDSCOUT_CONTINUATION_BULL_PENNANT = 3,
   GOLDSCOUT_CONTINUATION_BEAR_PENNANT = 4
};

struct GoldScoutContinuationPatternConfig
{
   double minPoleAtr;
   int    minPoleBars;
   int    maxPoleBars;
   double minPoleEfficiency;
   double minRetracementRatio;
   double maxRetracementRatio;
   int    minConsolidationBars;
   int    maxConsolidationBars;
   int    maxConfirmationBars;
   double minGeometryMoveAtr;
   double minFlagParallelRatio;
   double flagWidthTolerance;
   double maxPennantWidthRatio;
   double minPennantConvergenceBalance;
   double breakoutBufferAtr;
   double invalidationAtr;
   bool   useVolumeQuality;
   int    volumeLookback;
   double volumeMultiplier;
   bool   useMomentumQuality;
   double momentumBodyAtr;
};

struct GoldScoutContinuationPatternDiagnostic
{
   bool                                  detected;
   GoldScoutContinuationPatternType      type;
   GoldScoutPatternState                 state;
   GoldScoutPivot                        poleStart;
   GoldScoutPivot                        poleEnd;
   GoldScoutPivot                        correctionFirst;
   GoldScoutPivot                        correctionSecond;
   GoldScoutPivot                        correctionThird;
   datetime                              eventTime;
   int                                   poleBars;
   int                                   consolidationBars;
   int                                   barsAfterPattern;
   double                                referenceAtr;
   double                                poleLength;
   double                                poleStrengthAtr;
   double                                poleEfficiency;
   double                                retracementRatio;
   double                                upperSlope;
   double                                lowerSlope;
   double                                initialWidth;
   double                                finalWidth;
   double                                geometryQuality;
   double                                breakoutStrengthAtr;
   bool                                  volumeConfirmed;
   bool                                  momentumConfirmed;
   double                                quality;
   string                                identity;
};

enum GoldScoutConvergencePatternType
{
   GOLDSCOUT_CONVERGENCE_NONE                = 0,
   GOLDSCOUT_CONVERGENCE_ASC_TRIANGLE        = 1,
   GOLDSCOUT_CONVERGENCE_DESC_TRIANGLE       = 2,
   GOLDSCOUT_CONVERGENCE_SYMM_TRIANGLE       = 3,
   GOLDSCOUT_CONVERGENCE_RISING_WEDGE        = 4,
   GOLDSCOUT_CONVERGENCE_FALLING_WEDGE       = 5
};

struct GoldScoutConvergencePatternConfig
{
   int    minPatternBars;
   int    maxPatternBars;
   int    maxConfirmationBars;
   double horizontalSlopeAtrPerBar;
   double minSlopeAtrPerBar;
   double minSlopeSeparationAtrPerBar;
   double maxWidthRatio;
   double maxLineFitAtr;
   int    minBarsBeforeApex;
   int    minBreakoutBarsBeforeApex;
   double maxApexDistanceRatio;
   double breakoutBufferAtr;
   bool   useVolumeQuality;
   int    volumeLookback;
   double volumeMultiplier;
   bool   useMomentumQuality;
   double momentumBodyAtr;
};

struct GoldScoutConvergencePatternDiagnostic
{
   bool                                  detected;
   GoldScoutConvergencePatternType       type;
   GoldScoutPatternState                 state;
   GoldScoutPivot                        firstUpper;
   GoldScoutPivot                        secondUpper;
   GoldScoutPivot                        thirdUpper;
   GoldScoutPivot                        firstLower;
   GoldScoutPivot                        secondLower;
   GoldScoutPivot                        thirdLower;
   datetime                              eventTime;
   int                                   durationBars;
   int                                   barsAfterPattern;
   int                                   breakoutDirection;
   double                                referenceAtr;
   double                                upperSlope;
   double                                lowerSlope;
   double                                upperIntercept;
   double                                lowerIntercept;
   double                                initialWidth;
   double                                finalWidth;
   double                                widthRatio;
   double                                lineFitQuality;
   double                                slopeQuality;
   double                                convergenceQuality;
   double                                apexIndex;
   double                                apexPositionQuality;
   double                                breakoutStrengthAtr;
   bool                                  volumeConfirmed;
   bool                                  momentumConfirmed;
   double                                quality;
   string                                identity;
};

enum GoldScoutHeadShouldersPatternType
{
   GOLDSCOUT_HEAD_SHOULDERS_NONE     = 0,
   GOLDSCOUT_HEAD_SHOULDERS_HCH      = 1,
   GOLDSCOUT_HEAD_SHOULDERS_INVERTED = 2
};

struct GoldScoutHeadShouldersPatternConfig
{
   double shoulderToleranceAtr;
   double minHeadProminenceAtr;
   double minDepthAtr;
   int    minPivotBars;
   int    maxPivotBars;
   double minTemporalBalance;
   double maxNecklineSlopeAtrPerBar;
   int    maxConfirmationBars;
   double breakoutBufferAtr;
   double invalidationAtr;
   bool   useVolumeQuality;
   int    volumeLookback;
   double volumeMultiplier;
   bool   useMomentumQuality;
   double momentumBodyAtr;
};

struct GoldScoutHeadShouldersPatternDiagnostic
{
   bool                                  detected;
   GoldScoutHeadShouldersPatternType     type;
   GoldScoutPatternState                 state;
   GoldScoutPivot                        leftShoulder;
   GoldScoutPivot                        firstNeckline;
   GoldScoutPivot                        head;
   GoldScoutPivot                        secondNeckline;
   GoldScoutPivot                        rightShoulder;
   datetime                              eventTime;
   int                                   firstLegBars;
   int                                   secondLegBars;
   int                                   thirdLegBars;
   int                                   fourthLegBars;
   int                                   leftSpanBars;
   int                                   rightSpanBars;
   int                                   barsAfterPattern;
   int                                   breakoutDirection;
   double                                referenceAtr;
   double                                shoulderTolerance;
   double                                shoulderDifference;
   double                                headProminence;
   double                                depth;
   double                                temporalBalance;
   double                                necklineSlope;
   double                                necklineSlopeAtrPerBar;
   double                                necklineAtHead;
   double                                breakoutStrengthAtr;
   bool                                  volumeConfirmed;
   bool                                  momentumConfirmed;
   double                                quality;
   string                                identity;
};

struct GoldScoutBestPatternEvidence
{
   string name;
   string identity;
   int    direction;
   double quality;
   int    bonus;
};

const int GOLDSCOUT_MAX_STRUCTURAL_BUCKET_POINTS=25;
const int GOLDSCOUT_MAX_PATTERN_BONUS_POINTS=4;
const int GOLDSCOUT_MAX_M15_TIMING_POINTS=4;

void GS_ClearPivots(GoldScoutPivot &pivots[])
{
   ArrayResize(pivots,0);
}

void GS_ClearPivot(GoldScoutPivot &pivot)
{
   pivot.type=GOLDSCOUT_PIVOT_NONE;
   pivot.price=0.0;
   pivot.shift=0;
   pivot.time=0;
   pivot.atr=0.0;
   pivot.confirmed=false;
}

void GS_ClearM15TimingEvidence(GoldScoutM15TimingEvidence &evidence)
{
   evidence.available=false;
   evidence.closedBarTime=0;
   evidence.structure=GOLDSCOUT_STRUCTURE_INSUFFICIENT;
   evidence.haveLastSwingHigh=false;
   evidence.haveLastSwingLow=false;
   evidence.lastSwingHigh=0.0;
   evidence.lastSwingLow=0.0;
   evidence.breakoutDirection=0;
   evidence.recoveryDirection=0;
   evidence.momentumDirection=0;
   evidence.longAdjustment=0;
   evidence.shortAdjustment=0;
}

bool GS_ValidPositiveNumber(const double value)
{
   return MathIsValidNumber(value) && value>0.0;
}

bool GS_ValidatePivotConfig(const GoldScoutPivotConfig &config)
{
   return config.leftBars>=1 &&
          config.rightBars>=1 &&
          config.minBarsBetween>=1 &&
          MathIsValidNumber(config.minProminenceAtr) &&
          config.minProminenceAtr>=0.0 &&
          GS_ValidPositiveNumber(config.toleranceAtr);
}

// M15 is a soft timing layer. Opposing confirmed structure has precedence;
// breakout/recovery is one shared event so the same move cannot score twice.
int GS_M15TimingAdjustment(const bool available,
                           const GoldScoutStructureState structure,
                           const int direction,
                           const int breakoutDirection,
                           const int recoveryDirection,
                           const int momentumDirection)
{
   if(!available || (direction!=1 && direction!=-1)) return 0;
   if(structure!=GOLDSCOUT_STRUCTURE_BULLISH &&
      structure!=GOLDSCOUT_STRUCTURE_BEARISH)
      return 0;

   bool alignedStructure=(direction>0 && structure==GOLDSCOUT_STRUCTURE_BULLISH) ||
      (direction<0 && structure==GOLDSCOUT_STRUCTURE_BEARISH);
   bool opposingStructure=(direction>0 && structure==GOLDSCOUT_STRUCTURE_BEARISH) ||
      (direction<0 && structure==GOLDSCOUT_STRUCTURE_BULLISH);
   bool alignedEvent=(breakoutDirection==direction || recoveryDirection==direction);
   bool opposingEvent=(breakoutDirection==-direction || recoveryDirection==-direction);
   bool alignedMomentum=momentumDirection==direction;
   bool opposingMomentum=momentumDirection==-direction;

   if(opposingStructure)
      return (opposingEvent || opposingMomentum) ? -GOLDSCOUT_MAX_M15_TIMING_POINTS : -3;
   if(opposingEvent)
      return opposingMomentum ? -3 : -2;

   int points=0;
   if(alignedStructure) points+=1;
   if(alignedEvent) points+=2;
   if(alignedMomentum) points+=1;
   return (int)MathMax(-GOLDSCOUT_MAX_M15_TIMING_POINTS,
      MathMin(GOLDSCOUT_MAX_M15_TIMING_POINTS,points));
}

// A positive M15 adjustment is permitted only after H1 has independently
// reached the arming threshold. Missing M15 data or a weak H1 setup stays at 0.
int GS_GatedM15TimingAdjustment(const int h1TechnicalScore,
                                const int armThreshold,
                                const int requestedAdjustment)
{
   int bounded=(int)MathMax(-GOLDSCOUT_MAX_M15_TIMING_POINTS,
      MathMin(GOLDSCOUT_MAX_M15_TIMING_POINTS,requestedAdjustment));
   if(bounded>0 && h1TechnicalScore<armThreshold) return 0;
   return bounded;
}

void GS_LatestConfirmedSwingLevels(const GoldScoutPivot &confirmedPivots[],
                                   bool &haveHigh,double &lastHigh,
                                   bool &haveLow,double &lastLow)
{
   haveHigh=false;
   haveLow=false;
   lastHigh=0.0;
   lastLow=0.0;
   for(int i=ArraySize(confirmedPivots)-1;i>=0;i--)
   {
      if(!confirmedPivots[i].confirmed ||
         !GS_ValidPositiveNumber(confirmedPivots[i].price))
         continue;
      if(!haveHigh && confirmedPivots[i].type==GOLDSCOUT_PIVOT_HIGH)
      {
         haveHigh=true;
         lastHigh=confirmedPivots[i].price;
      }
      else if(!haveLow && confirmedPivots[i].type==GOLDSCOUT_PIVOT_LOW)
      {
         haveLow=true;
         lastLow=confirmedPivots[i].price;
      }
      if(haveHigh && haveLow) return;
   }
}

void GS_ClearStructureClassification(GoldScoutStructureClassification &classification)
{
   classification.state=GOLDSCOUT_STRUCTURE_INSUFFICIENT;
   classification.sufficient=false;
   classification.hh=false;
   classification.hl=false;
   classification.lh=false;
   classification.ll=false;
   classification.alternatingPivotCount=0;
}

void GS_ClearPatternDiagnostic(GoldScoutPatternDiagnostic &pattern)
{
   pattern.detected=false;
   pattern.type=GOLDSCOUT_PATTERN_NONE;
   pattern.state=GOLDSCOUT_PATTERN_STATE_NONE;
   GS_ClearPivot(pattern.first);
   GS_ClearPivot(pattern.neckline);
   GS_ClearPivot(pattern.second);
   pattern.eventTime=0;
   pattern.firstLegBars=0;
   pattern.secondLegBars=0;
   pattern.barsAfterSecond=0;
   pattern.referenceAtr=0.0;
   pattern.extremeTolerance=0.0;
   pattern.depth=0.0;
   pattern.breakoutStrengthAtr=0.0;
   pattern.volumeConfirmed=false;
   pattern.momentumConfirmed=false;
   pattern.quality=0.0;
   pattern.identity="";
}

string GS_StructureStateName(const GoldScoutStructureState state)
{
   if(state==GOLDSCOUT_STRUCTURE_BULLISH) return "ALCISTA";
   if(state==GOLDSCOUT_STRUCTURE_BEARISH) return "BAJISTA";
   if(state==GOLDSCOUT_STRUCTURE_NEUTRAL) return "NEUTRA / INDETERMINADA";
   return "INSUFICIENTE";
}

string GS_PatternTypeName(const GoldScoutPatternType type)
{
   if(type==GOLDSCOUT_PATTERN_W) return "W";
   if(type==GOLDSCOUT_PATTERN_M) return "M";
   return "NONE";
}

string GS_PatternStateName(const GoldScoutPatternState state)
{
   if(state==GOLDSCOUT_PATTERN_STATE_CANDIDATE) return "CANDIDATE";
   if(state==GOLDSCOUT_PATTERN_STATE_CONFIRMED) return "CONFIRMED";
   if(state==GOLDSCOUT_PATTERN_STATE_INVALIDATED) return "INVALIDATED";
   if(state==GOLDSCOUT_PATTERN_STATE_EXPIRED) return "EXPIRED";
   return "NONE";
}

string GS_PatternQualityName(const double quality)
{
   if(quality>=75.0) return "HIGH";
   if(quality>=50.0) return "MEDIUM";
   return "LOW";
}

bool GS_ValidatePatternConfig(const GoldScoutPatternConfig &config)
{
   return GS_ValidPositiveNumber(config.extremeToleranceAtr) &&
          GS_ValidPositiveNumber(config.minDepthAtr) &&
          config.minPivotBars>=1 &&
          config.maxPivotBars>=config.minPivotBars &&
          config.maxConfirmationBars>=1 &&
          MathIsValidNumber(config.breakoutBufferAtr) &&
          config.breakoutBufferAtr>=0.0 &&
          MathIsValidNumber(config.invalidationAtr) &&
          config.invalidationAtr>=0.0 &&
          GS_ValidPositiveNumber(config.minLegBalance) &&
          config.minLegBalance<=1.0 &&
          (!config.useVolumeQuality ||
             (config.volumeLookback>=1 && GS_ValidPositiveNumber(config.volumeMultiplier))) &&
          (!config.useMomentumQuality || GS_ValidPositiveNumber(config.momentumBodyAtr));
}

void GS_ClearContinuationPatternDiagnostic(GoldScoutContinuationPatternDiagnostic &pattern)
{
   pattern.detected=false;
   pattern.type=GOLDSCOUT_CONTINUATION_NONE;
   pattern.state=GOLDSCOUT_PATTERN_STATE_NONE;
   GS_ClearPivot(pattern.poleStart);
   GS_ClearPivot(pattern.poleEnd);
   GS_ClearPivot(pattern.correctionFirst);
   GS_ClearPivot(pattern.correctionSecond);
   GS_ClearPivot(pattern.correctionThird);
   pattern.eventTime=0;
   pattern.poleBars=0;
   pattern.consolidationBars=0;
   pattern.barsAfterPattern=0;
   pattern.referenceAtr=0.0;
   pattern.poleLength=0.0;
   pattern.poleStrengthAtr=0.0;
   pattern.poleEfficiency=0.0;
   pattern.retracementRatio=0.0;
   pattern.upperSlope=0.0;
   pattern.lowerSlope=0.0;
   pattern.initialWidth=0.0;
   pattern.finalWidth=0.0;
   pattern.geometryQuality=0.0;
   pattern.breakoutStrengthAtr=0.0;
   pattern.volumeConfirmed=false;
   pattern.momentumConfirmed=false;
   pattern.quality=0.0;
   pattern.identity="";
}

void GS_ClearConvergencePatternDiagnostic(GoldScoutConvergencePatternDiagnostic &pattern)
{
   pattern.detected=false;
   pattern.type=GOLDSCOUT_CONVERGENCE_NONE;
   pattern.state=GOLDSCOUT_PATTERN_STATE_NONE;
   GS_ClearPivot(pattern.firstUpper);
   GS_ClearPivot(pattern.secondUpper);
   GS_ClearPivot(pattern.thirdUpper);
   GS_ClearPivot(pattern.firstLower);
   GS_ClearPivot(pattern.secondLower);
   GS_ClearPivot(pattern.thirdLower);
   pattern.eventTime=0;
   pattern.durationBars=0;
   pattern.barsAfterPattern=0;
   pattern.breakoutDirection=0;
   pattern.referenceAtr=0.0;
   pattern.upperSlope=0.0;
   pattern.lowerSlope=0.0;
   pattern.upperIntercept=0.0;
   pattern.lowerIntercept=0.0;
   pattern.initialWidth=0.0;
   pattern.finalWidth=0.0;
   pattern.widthRatio=0.0;
   pattern.lineFitQuality=0.0;
   pattern.slopeQuality=0.0;
   pattern.convergenceQuality=0.0;
   pattern.apexIndex=0.0;
   pattern.apexPositionQuality=0.0;
   pattern.breakoutStrengthAtr=0.0;
   pattern.volumeConfirmed=false;
   pattern.momentumConfirmed=false;
   pattern.quality=0.0;
   pattern.identity="";
}

void GS_ClearHeadShouldersPatternDiagnostic(GoldScoutHeadShouldersPatternDiagnostic &pattern)
{
   pattern.detected=false;
   pattern.type=GOLDSCOUT_HEAD_SHOULDERS_NONE;
   pattern.state=GOLDSCOUT_PATTERN_STATE_NONE;
   GS_ClearPivot(pattern.leftShoulder);
   GS_ClearPivot(pattern.firstNeckline);
   GS_ClearPivot(pattern.head);
   GS_ClearPivot(pattern.secondNeckline);
   GS_ClearPivot(pattern.rightShoulder);
   pattern.eventTime=0;
   pattern.firstLegBars=0;
   pattern.secondLegBars=0;
   pattern.thirdLegBars=0;
   pattern.fourthLegBars=0;
   pattern.leftSpanBars=0;
   pattern.rightSpanBars=0;
   pattern.barsAfterPattern=0;
   pattern.breakoutDirection=0;
   pattern.referenceAtr=0.0;
   pattern.shoulderTolerance=0.0;
   pattern.shoulderDifference=0.0;
   pattern.headProminence=0.0;
   pattern.depth=0.0;
   pattern.temporalBalance=0.0;
   pattern.necklineSlope=0.0;
   pattern.necklineSlopeAtrPerBar=0.0;
   pattern.necklineAtHead=0.0;
   pattern.breakoutStrengthAtr=0.0;
   pattern.volumeConfirmed=false;
   pattern.momentumConfirmed=false;
   pattern.quality=0.0;
   pattern.identity="";
}

string GS_ContinuationPatternTypeName(const GoldScoutContinuationPatternType type)
{
   if(type==GOLDSCOUT_CONTINUATION_BULL_FLAG) return "BULL_FLAG";
   if(type==GOLDSCOUT_CONTINUATION_BEAR_FLAG) return "BEAR_FLAG";
   if(type==GOLDSCOUT_CONTINUATION_BULL_PENNANT) return "BULL_PENNANT";
   if(type==GOLDSCOUT_CONTINUATION_BEAR_PENNANT) return "BEAR_PENNANT";
   return "NONE";
}

bool GS_ValidateContinuationPatternConfig(const GoldScoutContinuationPatternConfig &config)
{
   return GS_ValidPositiveNumber(config.minPoleAtr) &&
          config.minPoleBars>=1 && config.maxPoleBars>=config.minPoleBars &&
          GS_ValidPositiveNumber(config.minPoleEfficiency) &&
          config.minPoleEfficiency<=1.0 &&
          GS_ValidPositiveNumber(config.minRetracementRatio) &&
          config.maxRetracementRatio>config.minRetracementRatio &&
          config.maxRetracementRatio<1.0 &&
          config.minConsolidationBars>=1 &&
          config.maxConsolidationBars>=config.minConsolidationBars &&
          config.maxConfirmationBars>=1 &&
          GS_ValidPositiveNumber(config.minGeometryMoveAtr) &&
          GS_ValidPositiveNumber(config.minFlagParallelRatio) &&
          config.minFlagParallelRatio<=1.0 &&
          GS_ValidPositiveNumber(config.flagWidthTolerance) &&
          config.flagWidthTolerance<1.0 &&
          GS_ValidPositiveNumber(config.maxPennantWidthRatio) &&
          config.maxPennantWidthRatio<1.0 &&
          GS_ValidPositiveNumber(config.minPennantConvergenceBalance) &&
          config.minPennantConvergenceBalance<=1.0 &&
          MathIsValidNumber(config.breakoutBufferAtr) &&
          config.breakoutBufferAtr>=0.0 &&
          MathIsValidNumber(config.invalidationAtr) &&
          config.invalidationAtr>=0.0 &&
          (!config.useVolumeQuality ||
             (config.volumeLookback>=1 && GS_ValidPositiveNumber(config.volumeMultiplier))) &&
          (!config.useMomentumQuality || GS_ValidPositiveNumber(config.momentumBodyAtr));
}

string GS_ConvergencePatternTypeName(const GoldScoutConvergencePatternType type)
{
   if(type==GOLDSCOUT_CONVERGENCE_ASC_TRIANGLE) return "ASC_TRIANGLE";
   if(type==GOLDSCOUT_CONVERGENCE_DESC_TRIANGLE) return "DESC_TRIANGLE";
   if(type==GOLDSCOUT_CONVERGENCE_SYMM_TRIANGLE) return "SYMM_TRIANGLE";
   if(type==GOLDSCOUT_CONVERGENCE_RISING_WEDGE) return "RISING_WEDGE";
   if(type==GOLDSCOUT_CONVERGENCE_FALLING_WEDGE) return "FALLING_WEDGE";
   return "NONE";
}

bool GS_ValidateConvergencePatternConfig(const GoldScoutConvergencePatternConfig &config)
{
   return config.minPatternBars>=5 &&
          config.maxPatternBars>=config.minPatternBars &&
          config.maxConfirmationBars>=1 &&
          GS_ValidPositiveNumber(config.horizontalSlopeAtrPerBar) &&
          GS_ValidPositiveNumber(config.minSlopeAtrPerBar) &&
          config.minSlopeAtrPerBar>config.horizontalSlopeAtrPerBar &&
          GS_ValidPositiveNumber(config.minSlopeSeparationAtrPerBar) &&
          GS_ValidPositiveNumber(config.maxWidthRatio) &&
          config.maxWidthRatio<1.0 &&
          GS_ValidPositiveNumber(config.maxLineFitAtr) &&
          config.minBarsBeforeApex>=1 &&
          config.minBreakoutBarsBeforeApex>=0 &&
          GS_ValidPositiveNumber(config.maxApexDistanceRatio) &&
          MathIsValidNumber(config.breakoutBufferAtr) &&
          config.breakoutBufferAtr>=0.0 &&
          (!config.useVolumeQuality ||
             (config.volumeLookback>=1 && GS_ValidPositiveNumber(config.volumeMultiplier))) &&
          (!config.useMomentumQuality || GS_ValidPositiveNumber(config.momentumBodyAtr));
}

string GS_HeadShouldersPatternTypeName(const GoldScoutHeadShouldersPatternType type)
{
   if(type==GOLDSCOUT_HEAD_SHOULDERS_HCH) return "HCH";
   if(type==GOLDSCOUT_HEAD_SHOULDERS_INVERTED) return "HCH_INVERTED";
   return "NONE";
}

bool GS_ValidateHeadShouldersPatternConfig(
   const GoldScoutHeadShouldersPatternConfig &config)
{
   return GS_ValidPositiveNumber(config.shoulderToleranceAtr) &&
          GS_ValidPositiveNumber(config.minHeadProminenceAtr) &&
          GS_ValidPositiveNumber(config.minDepthAtr) &&
          config.minPivotBars>=1 &&
          config.maxPivotBars>=config.minPivotBars &&
          GS_ValidPositiveNumber(config.minTemporalBalance) &&
          config.minTemporalBalance<=1.0 &&
          GS_ValidPositiveNumber(config.maxNecklineSlopeAtrPerBar) &&
          config.maxConfirmationBars>=1 &&
          MathIsValidNumber(config.breakoutBufferAtr) &&
          config.breakoutBufferAtr>=0.0 &&
          MathIsValidNumber(config.invalidationAtr) &&
          config.invalidationAtr>=0.0 &&
          (!config.useVolumeQuality ||
             (config.volumeLookback>=1 && GS_ValidPositiveNumber(config.volumeMultiplier))) &&
          (!config.useMomentumQuality || GS_ValidPositiveNumber(config.momentumBodyAtr));
}

void GS_ClearBestPatternEvidence(GoldScoutBestPatternEvidence &evidence)
{
   evidence.name="NONE";
   evidence.identity="";
   evidence.direction=0;
   evidence.quality=0.0;
   evidence.bonus=0;
}

int GS_PatternQualityBonus(const double quality)
{
   if(!MathIsValidNumber(quality) || quality<50.0) return 0;
   if(quality<65.0) return 1;
   if(quality<75.0) return 2;
   if(quality<85.0) return 3;
   return GOLDSCOUT_MAX_PATTERN_BONUS_POINTS;
}

void GS_UpdateBestPatternEvidence(const string name,const string identity,
                                  const int direction,const double quality,
                                  GoldScoutBestPatternEvidence &best)
{
   if(name=="" || identity=="" || (direction!=1 && direction!=-1) ||
      !MathIsValidNumber(quality))
      return;
   double boundedQuality=MathMax(0.0,MathMin(100.0,quality));
   int bonus=GS_PatternQualityBonus(boundedQuality);
   if(bonus<=0 || boundedQuality<=best.quality) return;
   best.name=name;
   best.identity=identity;
   best.direction=direction;
   best.quality=boundedQuality;
   best.bonus=bonus;
}

void GS_ConsiderConfirmedPatternEvidence(
   const string name,const string identity,const GoldScoutPatternState state,
   const int direction,const double quality,
   GoldScoutBestPatternEvidence &bestLong,
   GoldScoutBestPatternEvidence &bestShort)
{
   if(state!=GOLDSCOUT_PATTERN_STATE_CONFIRMED) return;
   if(direction>0)
      GS_UpdateBestPatternEvidence(name,identity,1,quality,bestLong);
   else if(direction<0)
      GS_UpdateBestPatternEvidence(name,identity,-1,quality,bestShort);
}

// A pattern opposed by an explicit confirmed structure receives no bonus. A
// neutral/insufficient structure can still receive the soft pattern evidence;
// this never blocks either direction and is capped independently at four.
int GS_AllowedPatternBonus(const GoldScoutStructureState state,
                           const int direction,const int requestedBonus)
{
   if(direction==0) return 0;
   bool contradicted=(direction>0 && state==GOLDSCOUT_STRUCTURE_BEARISH) ||
      (direction<0 && state==GOLDSCOUT_STRUCTURE_BULLISH);
   if(contradicted) return 0;
   return (int)MathMax(0,MathMin(GOLDSCOUT_MAX_PATTERN_BONUS_POINTS,
                                 requestedBonus));
}

// The structural bucket is directional and deliberately capped. Pullback adds
// only confirmation value on top of an already-confirmed structure. Momentum
// is not counted separately when it represents the same impulse as breakout.
int GS_StructuralBucketPoints(const GoldScoutStructureState state,
                              const int direction,
                              const bool pullback,
                              const bool breakout,
                              const bool momentum,
                              const int requestedPatternBonus=0)
{
   if(direction==0) return 0;
   bool aligned=(direction>0 && state==GOLDSCOUT_STRUCTURE_BULLISH) ||
      (direction<0 && state==GOLDSCOUT_STRUCTURE_BEARISH);
   int points=0;
   if(aligned)
   {
      points=15;
      if(pullback) points+=5;
      if(breakout) points+=10;
      else if(momentum) points+=5;
   }
   points+=GS_AllowedPatternBonus(state,direction,requestedPatternBonus);
   return (int)MathMin(GOLDSCOUT_MAX_STRUCTURAL_BUCKET_POINTS,points);
}

// Shared future tolerance for matching structural levels (for example, two
// lows or two highs). Both ATR inputs and the price step must be trustworthy.
bool GS_ATRTolerance(const double firstAtr,const double secondAtr,
                     const double multiplier,const double minimumPriceStep,
                     double &tolerance)
{
   tolerance=0.0;
   if(!GS_ValidPositiveNumber(firstAtr) ||
      !GS_ValidPositiveNumber(secondAtr) ||
      !GS_ValidPositiveNumber(multiplier) ||
      !GS_ValidPositiveNumber(minimumPriceStep))
      return false;

   tolerance=MathMax(minimumPriceStep,MathMax(firstAtr,secondAtr)*multiplier);
   return GS_ValidPositiveNumber(tolerance);
}

// Produce an alternating chronological sequence. Consecutive confirmed pivots
// of the same type collapse to the more extreme one; no open/unconfirmed pivot
// can replace a confirmed structural point.
bool GS_NormalizeAlternatingPivots(const GoldScoutPivot &confirmedPivots[],
                                   GoldScoutPivot &alternatingPivots[])
{
   GS_ClearPivots(alternatingPivots);
   bool havePrevious=false;
   datetime previousTime=0;
   int previousShift=0;

   int count=ArraySize(confirmedPivots);
   for(int i=0;i<count;i++)
   {
      if(!confirmedPivots[i].confirmed) continue;
      if((confirmedPivots[i].type!=GOLDSCOUT_PIVOT_HIGH &&
          confirmedPivots[i].type!=GOLDSCOUT_PIVOT_LOW) ||
         !GS_ValidPositiveNumber(confirmedPivots[i].price) ||
         !GS_ValidPositiveNumber(confirmedPivots[i].atr) ||
         confirmedPivots[i].time<=0 || confirmedPivots[i].shift<1)
      {
         GS_ClearPivots(alternatingPivots);
         return false;
      }
      if(havePrevious &&
         (confirmedPivots[i].time<=previousTime || confirmedPivots[i].shift>=previousShift))
      {
         GS_ClearPivots(alternatingPivots);
         return false;
      }
      havePrevious=true;
      previousTime=confirmedPivots[i].time;
      previousShift=confirmedPivots[i].shift;

      int size=ArraySize(alternatingPivots);
      if(size>0 && alternatingPivots[size-1].type==confirmedPivots[i].type)
      {
         bool moreExtreme=(confirmedPivots[i].type==GOLDSCOUT_PIVOT_HIGH)
            ? confirmedPivots[i].price>alternatingPivots[size-1].price
            : confirmedPivots[i].price<alternatingPivots[size-1].price;
         if(moreExtreme) alternatingPivots[size-1]=confirmedPivots[i];
         continue;
      }

      if(ArrayResize(alternatingPivots,size+1)!=size+1)
      {
         GS_ClearPivots(alternatingPivots);
         return false;
      }
      alternatingPivots[size]=confirmedPivots[i];
   }
   return true;
}

bool GS_ClassifyConfirmedStructure(const GoldScoutPivot &confirmedPivots[],
                                   const double toleranceAtr,
                                   const double minimumPriceStep,
                                   GoldScoutStructureClassification &classification)
{
   GS_ClearStructureClassification(classification);
   GoldScoutPivot alternating[];
   if(!GS_NormalizeAlternatingPivots(confirmedPivots,alternating)) return false;
   classification.alternatingPivotCount=ArraySize(alternating);

   GoldScoutPivot currentHigh,previousHigh,currentLow,previousLow;
   GS_ClearPivot(currentHigh);
   GS_ClearPivot(previousHigh);
   GS_ClearPivot(currentLow);
   GS_ClearPivot(previousLow);
   bool haveCurrentHigh=false,havePreviousHigh=false;
   bool haveCurrentLow=false,havePreviousLow=false;
   for(int i=ArraySize(alternating)-1;i>=0;i--)
   {
      if(alternating[i].type==GOLDSCOUT_PIVOT_HIGH)
      {
         if(!haveCurrentHigh)
         {
            currentHigh=alternating[i];
            haveCurrentHigh=true;
         }
         else if(!havePreviousHigh)
         {
            previousHigh=alternating[i];
            havePreviousHigh=true;
         }
      }
      else if(alternating[i].type==GOLDSCOUT_PIVOT_LOW)
      {
         if(!haveCurrentLow)
         {
            currentLow=alternating[i];
            haveCurrentLow=true;
         }
         else if(!havePreviousLow)
         {
            previousLow=alternating[i];
            havePreviousLow=true;
         }
      }
      if(havePreviousHigh && havePreviousLow) break;
   }

   if(!havePreviousHigh || !havePreviousLow) return true;

   double highTolerance=0.0,lowTolerance=0.0;
   if(!GS_ATRTolerance(previousHigh.atr,currentHigh.atr,toleranceAtr,minimumPriceStep,highTolerance) ||
      !GS_ATRTolerance(previousLow.atr,currentLow.atr,toleranceAtr,minimumPriceStep,lowTolerance))
      return false;

   classification.sufficient=true;
   classification.hh=currentHigh.price>previousHigh.price+highTolerance;
   classification.lh=currentHigh.price<previousHigh.price-highTolerance;
   classification.hl=currentLow.price>previousLow.price+lowTolerance;
   classification.ll=currentLow.price<previousLow.price-lowTolerance;

   if(classification.hh && classification.hl)
      classification.state=GOLDSCOUT_STRUCTURE_BULLISH;
   else if(classification.lh && classification.ll)
      classification.state=GOLDSCOUT_STRUCTURE_BEARISH;
   else
      classification.state=GOLDSCOUT_STRUCTURE_NEUTRAL;
   return true;
}

int GS_FindClosedRateByTime(const MqlRates &closedRates[],const datetime pivotTime)
{
   for(int i=0;i<ArraySize(closedRates);i++)
      if(closedRates[i].time==pivotTime) return i;
   return -1;
}

bool GS_ClosedRatesValid(const MqlRates &closedRates[])
{
   int count=ArraySize(closedRates);
   for(int i=0;i<count;i++)
   {
      if(closedRates[i].time<=0 ||
         !GS_ValidPositiveNumber(closedRates[i].open) ||
         !GS_ValidPositiveNumber(closedRates[i].high) ||
         !GS_ValidPositiveNumber(closedRates[i].low) ||
         !GS_ValidPositiveNumber(closedRates[i].close) ||
         closedRates[i].high<MathMax(closedRates[i].open,closedRates[i].close) ||
         closedRates[i].low>MathMin(closedRates[i].open,closedRates[i].close) ||
         closedRates[i].tick_volume<0)
         return false;
      if(i>0 && closedRates[i].time<=closedRates[i-1].time) return false;
   }
   return true;
}

bool GS_PatternVolumeConfirmation(const MqlRates &closedRates[],
                                  const int breakoutIndex,
                                  const GoldScoutPatternConfig &config)
{
   if(!config.useVolumeQuality || breakoutIndex<=0) return false;
   int first=MathMax(0,breakoutIndex-config.volumeLookback);
   double total=0.0;
   int samples=0;
   for(int i=first;i<breakoutIndex;i++)
   {
      if(closedRates[i].tick_volume<=0) continue;
      total+=(double)closedRates[i].tick_volume;
      samples++;
   }
   if(samples<1 || closedRates[breakoutIndex].tick_volume<=0) return false;
   return (double)closedRates[breakoutIndex].tick_volume >=
      (total/(double)samples)*config.volumeMultiplier;
}

bool GS_PatternMomentumConfirmation(const MqlRates &bar,
                                    const GoldScoutPatternType type,
                                    const double referenceAtr,
                                    const GoldScoutPatternConfig &config)
{
   if(!config.useMomentumQuality || !GS_ValidPositiveNumber(referenceAtr)) return false;
   double directionalBody=(type==GOLDSCOUT_PATTERN_W)
      ? bar.close-bar.open
      : bar.open-bar.close;
   return directionalBody>=config.momentumBodyAtr*referenceAtr;
}

double GS_PatternQuality(const GoldScoutPatternDiagnostic &pattern,
                         const GoldScoutPatternConfig &config)
{
   if(!GS_ValidatePatternConfig(config) || !pattern.detected ||
      !GS_ValidPositiveNumber(pattern.referenceAtr) ||
      !GS_ValidPositiveNumber(pattern.extremeTolerance))
      return 0.0;

   double extremeDifference=MathAbs(pattern.first.price-pattern.second.price);
   double similarity=MathMax(0.0,MathMin(1.0,
      1.0-extremeDifference/pattern.extremeTolerance));
   double minimumDepth=config.minDepthAtr*pattern.referenceAtr;
   double depthScore=MathMax(0.0,MathMin(1.0,
      pattern.depth/(2.0*minimumDepth)));
   double legBalance=(double)MathMin(pattern.firstLegBars,pattern.secondLegBars)/
      (double)MathMax(pattern.firstLegBars,pattern.secondLegBars);
   double breakoutScore=0.0;
   if(pattern.state==GOLDSCOUT_PATTERN_STATE_CONFIRMED)
      breakoutScore=MathMax(0.0,MathMin(1.0,pattern.breakoutStrengthAtr));

   double quality=25.0*similarity + 25.0*depthScore +
      20.0*legBalance + 20.0*breakoutScore;
   if(pattern.volumeConfirmed) quality+=5.0;
   if(pattern.momentumConfirmed) quality+=5.0;
   return MathMax(0.0,MathMin(100.0,quality));
}

// Detect the newest geometrically-valid W or M from confirmed alternating
// pivots. closedRates must be oldest-to-newest and must exclude the open bar.
// The first close event after the second extreme is terminal for that identity:
// a confirmed pattern therefore cannot repaint on later closed candles.
bool GS_DetectLatestConfirmedPattern(const GoldScoutPivot &confirmedPivots[],
                                     const MqlRates &closedRates[],
                                     const int newestClosedShift,
                                     const GoldScoutPatternConfig &config,
                                     const double minimumPriceStep,
                                     GoldScoutPatternDiagnostic &pattern)
{
   GS_ClearPatternDiagnostic(pattern);
   if(newestClosedShift<1 || !GS_ValidatePatternConfig(config) ||
      !GS_ValidPositiveNumber(minimumPriceStep) ||
      ArraySize(closedRates)<3 || !GS_ClosedRatesValid(closedRates))
      return false;

   GoldScoutPivot alternating[];
   if(!GS_NormalizeAlternatingPivots(confirmedPivots,alternating)) return false;

   for(int i=ArraySize(alternating)-3;i>=0;i--)
   {
      GoldScoutPivot first=alternating[i];
      GoldScoutPivot neckline=alternating[i+1];
      GoldScoutPivot second=alternating[i+2];
      GoldScoutPatternType type=GOLDSCOUT_PATTERN_NONE;
      if(first.type==GOLDSCOUT_PIVOT_LOW &&
         neckline.type==GOLDSCOUT_PIVOT_HIGH &&
         second.type==GOLDSCOUT_PIVOT_LOW)
         type=GOLDSCOUT_PATTERN_W;
      else if(first.type==GOLDSCOUT_PIVOT_HIGH &&
              neckline.type==GOLDSCOUT_PIVOT_LOW &&
              second.type==GOLDSCOUT_PIVOT_HIGH)
         type=GOLDSCOUT_PATTERN_M;
      else
         continue;

      int firstRateIndex=GS_FindClosedRateByTime(closedRates,first.time);
      int necklineRateIndex=GS_FindClosedRateByTime(closedRates,neckline.time);
      int secondRateIndex=GS_FindClosedRateByTime(closedRates,second.time);
      if(firstRateIndex<0 || necklineRateIndex<=firstRateIndex ||
         secondRateIndex<=necklineRateIndex)
         continue;
      int expectedFirstShift=newestClosedShift+(ArraySize(closedRates)-1-firstRateIndex);
      int expectedNecklineShift=newestClosedShift+(ArraySize(closedRates)-1-necklineRateIndex);
      int expectedSecondShift=newestClosedShift+(ArraySize(closedRates)-1-secondRateIndex);
      if(first.shift!=expectedFirstShift || neckline.shift!=expectedNecklineShift ||
         second.shift!=expectedSecondShift)
         continue;

      int firstLeg=first.shift-neckline.shift;
      int secondLeg=neckline.shift-second.shift;
      if(firstLeg<config.minPivotBars || secondLeg<config.minPivotBars ||
         firstLeg>config.maxPivotBars || secondLeg>config.maxPivotBars)
         continue;
      double legBalance=(double)MathMin(firstLeg,secondLeg)/
         (double)MathMax(firstLeg,secondLeg);
      if(legBalance<config.minLegBalance) continue;

      double tolerance=0.0;
      if(!GS_ATRTolerance(first.atr,second.atr,config.extremeToleranceAtr,
                          minimumPriceStep,tolerance))
         return false;
      if(MathAbs(first.price-second.price)>tolerance) continue;

      double referenceAtr=MathMax(first.atr,MathMax(neckline.atr,second.atr));
      if(!GS_ValidPositiveNumber(referenceAtr)) return false;
      double depth=(type==GOLDSCOUT_PATTERN_W)
         ? MathMin(neckline.price-first.price,neckline.price-second.price)
         : MathMin(first.price-neckline.price,second.price-neckline.price);
      if(depth<config.minDepthAtr*referenceAtr) continue;

      pattern.detected=true;
      pattern.type=type;
      pattern.state=GOLDSCOUT_PATTERN_STATE_CANDIDATE;
      pattern.first=first;
      pattern.neckline=neckline;
      pattern.second=second;
      pattern.firstLegBars=firstLeg;
      pattern.secondLegBars=secondLeg;
      pattern.referenceAtr=referenceAtr;
      pattern.extremeTolerance=tolerance;
      pattern.depth=depth;
      pattern.identity=StringFormat("%d:%I64d:%I64d:%I64d",(int)type,
         (long)first.time,(long)neckline.time,(long)second.time);

      double invalidationDistance=config.invalidationAtr*referenceAtr;
      double breakoutDistance=config.breakoutBufferAtr*referenceAtr;
      for(int barIndex=secondRateIndex+1;barIndex<ArraySize(closedRates);barIndex++)
      {
         pattern.barsAfterSecond++;
         if(pattern.barsAfterSecond>config.maxConfirmationBars)
         {
            pattern.state=GOLDSCOUT_PATTERN_STATE_EXPIRED;
            pattern.eventTime=closedRates[barIndex].time;
            break;
         }

         bool invalidated=(type==GOLDSCOUT_PATTERN_W)
            ? closedRates[barIndex].close<MathMin(first.price,second.price)-invalidationDistance
            : closedRates[barIndex].close>MathMax(first.price,second.price)+invalidationDistance;
         if(invalidated)
         {
            pattern.state=GOLDSCOUT_PATTERN_STATE_INVALIDATED;
            pattern.eventTime=closedRates[barIndex].time;
            break;
         }

         bool confirmed=(type==GOLDSCOUT_PATTERN_W)
            ? closedRates[barIndex].close>neckline.price+breakoutDistance
            : closedRates[barIndex].close<neckline.price-breakoutDistance;
         if(confirmed)
         {
            pattern.state=GOLDSCOUT_PATTERN_STATE_CONFIRMED;
            pattern.eventTime=closedRates[barIndex].time;
            pattern.breakoutStrengthAtr=(type==GOLDSCOUT_PATTERN_W)
               ? (closedRates[barIndex].close-neckline.price)/referenceAtr
               : (neckline.price-closedRates[barIndex].close)/referenceAtr;
            pattern.volumeConfirmed=GS_PatternVolumeConfirmation(closedRates,barIndex,config);
            pattern.momentumConfirmed=GS_PatternMomentumConfirmation(
               closedRates[barIndex],type,referenceAtr,config);
            break;
         }
      }

      pattern.quality=GS_PatternQuality(pattern,config);
      return true;
   }
   return true;
}

bool GS_ContinuationPoleEfficiency(const MqlRates &closedRates[],
                                   const int startIndex,
                                   const int endIndex,
                                   const bool bullish,
                                   double &efficiency)
{
   efficiency=0.0;
   if(startIndex<0 || endIndex<=startIndex || endIndex>=ArraySize(closedRates))
      return false;
   double path=0.0;
   for(int i=startIndex+1;i<=endIndex;i++)
      path+=MathAbs(closedRates[i].close-closedRates[i-1].close);
   double directionalMove=bullish
      ? closedRates[endIndex].close-closedRates[startIndex].close
      : closedRates[startIndex].close-closedRates[endIndex].close;
   if(!GS_ValidPositiveNumber(path) || !GS_ValidPositiveNumber(directionalMove))
      return true;
   efficiency=MathMax(0.0,MathMin(1.0,directionalMove/path));
   return true;
}

bool GS_ContinuationVolumeConfirmation(const MqlRates &closedRates[],
                                       const int breakoutIndex,
                                       const GoldScoutContinuationPatternConfig &config)
{
   if(!config.useVolumeQuality || breakoutIndex<=0) return false;
   int first=(int)MathMax(0,breakoutIndex-config.volumeLookback);
   double total=0.0;
   int samples=0;
   for(int i=first;i<breakoutIndex;i++)
   {
      if(closedRates[i].tick_volume<=0) continue;
      total+=(double)closedRates[i].tick_volume;
      samples++;
   }
   if(samples<1 || closedRates[breakoutIndex].tick_volume<=0) return false;
   return (double)closedRates[breakoutIndex].tick_volume >=
      (total/(double)samples)*config.volumeMultiplier;
}

bool GS_ContinuationMomentumConfirmation(const MqlRates &bar,
                                         const bool bullish,
                                         const double referenceAtr,
                                         const GoldScoutContinuationPatternConfig &config)
{
   if(!config.useMomentumQuality || !GS_ValidPositiveNumber(referenceAtr)) return false;
   double directionalBody=bullish ? bar.close-bar.open : bar.open-bar.close;
   return directionalBody>=config.momentumBodyAtr*referenceAtr;
}

double GS_ContinuationPatternQuality(
   const GoldScoutContinuationPatternDiagnostic &pattern,
   const GoldScoutContinuationPatternConfig &config)
{
   if(!GS_ValidateContinuationPatternConfig(config) || !pattern.detected ||
      !GS_ValidPositiveNumber(pattern.referenceAtr) ||
      !GS_ValidPositiveNumber(pattern.poleStrengthAtr))
      return 0.0;

   double poleStrength=MathMax(0.0,MathMin(1.0,
      pattern.poleStrengthAtr/(2.0*config.minPoleAtr)));
   double poleFactor=(poleStrength+pattern.poleEfficiency)/2.0;

   double idealRetracement=(config.minRetracementRatio+config.maxRetracementRatio)/2.0;
   double retracementHalfRange=(config.maxRetracementRatio-config.minRetracementRatio)/2.0;
   double retracementFactor=MathMax(0.0,MathMin(1.0,
      1.0-MathAbs(pattern.retracementRatio-idealRetracement)/retracementHalfRange));

   double idealDuration=((double)config.minConsolidationBars+
      (double)config.maxConsolidationBars)/2.0;
   double durationHalfRange=MathMax(0.5,
      ((double)config.maxConsolidationBars-(double)config.minConsolidationBars)/2.0);
   double durationFactor=MathMax(0.0,MathMin(1.0,
      1.0-MathAbs((double)pattern.consolidationBars-idealDuration)/durationHalfRange));

   double breakoutFactor=0.0;
   if(pattern.state==GOLDSCOUT_PATTERN_STATE_CONFIRMED)
      breakoutFactor=MathMax(0.0,MathMin(1.0,pattern.breakoutStrengthAtr));

   double quality=25.0*poleFactor + 15.0*retracementFactor +
      10.0*durationFactor + 25.0*MathMax(0.0,MathMin(1.0,pattern.geometryQuality)) +
      15.0*breakoutFactor;
   if(pattern.volumeConfirmed) quality+=5.0;
   if(pattern.momentumConfirmed) quality+=5.0;
   return MathMax(0.0,MathMin(100.0,quality));
}

// Detect exactly one newest continuation identity from five adjacent confirmed
// alternating pivots: pole start/end plus three corrective pivots. All state
// transitions use closedRates only, and the first terminal close is immutable.
bool GS_DetectLatestContinuationPattern(
   const GoldScoutPivot &confirmedPivots[],
   const MqlRates &closedRates[],
   const int newestClosedShift,
   const GoldScoutContinuationPatternConfig &config,
   const double minimumPriceStep,
   GoldScoutContinuationPatternDiagnostic &pattern)
{
   GS_ClearContinuationPatternDiagnostic(pattern);
   if(newestClosedShift<1 || !GS_ValidateContinuationPatternConfig(config) ||
      !GS_ValidPositiveNumber(minimumPriceStep) ||
      ArraySize(closedRates)<5 || !GS_ClosedRatesValid(closedRates))
      return false;

   GoldScoutPivot alternating[];
   if(!GS_NormalizeAlternatingPivots(confirmedPivots,alternating)) return false;

   for(int i=ArraySize(alternating)-5;i>=0;i--)
   {
      GoldScoutPivot p0=alternating[i];
      GoldScoutPivot p1=alternating[i+1];
      GoldScoutPivot p2=alternating[i+2];
      GoldScoutPivot p3=alternating[i+3];
      GoldScoutPivot p4=alternating[i+4];
      bool bullish=p0.type==GOLDSCOUT_PIVOT_LOW &&
         p1.type==GOLDSCOUT_PIVOT_HIGH && p2.type==GOLDSCOUT_PIVOT_LOW &&
         p3.type==GOLDSCOUT_PIVOT_HIGH && p4.type==GOLDSCOUT_PIVOT_LOW;
      bool bearish=p0.type==GOLDSCOUT_PIVOT_HIGH &&
         p1.type==GOLDSCOUT_PIVOT_LOW && p2.type==GOLDSCOUT_PIVOT_HIGH &&
         p3.type==GOLDSCOUT_PIVOT_LOW && p4.type==GOLDSCOUT_PIVOT_HIGH;
      if(!bullish && !bearish) continue;

      int rateIndex[5];
      rateIndex[0]=GS_FindClosedRateByTime(closedRates,p0.time);
      rateIndex[1]=GS_FindClosedRateByTime(closedRates,p1.time);
      rateIndex[2]=GS_FindClosedRateByTime(closedRates,p2.time);
      rateIndex[3]=GS_FindClosedRateByTime(closedRates,p3.time);
      rateIndex[4]=GS_FindClosedRateByTime(closedRates,p4.time);
      if(rateIndex[0]<0 || rateIndex[1]<=rateIndex[0] ||
         rateIndex[2]<=rateIndex[1] || rateIndex[3]<=rateIndex[2] ||
         rateIndex[4]<=rateIndex[3])
         continue;

      GoldScoutPivot sequence[5];
      sequence[0]=p0; sequence[1]=p1; sequence[2]=p2;
      sequence[3]=p3; sequence[4]=p4;
      bool shiftsMatch=true;
      for(int pivotIndex=0;pivotIndex<5;pivotIndex++)
      {
         int expectedShift=newestClosedShift+
            (ArraySize(closedRates)-1-rateIndex[pivotIndex]);
         if(sequence[pivotIndex].shift!=expectedShift)
         {
            shiftsMatch=false;
            break;
         }
      }
      if(!shiftsMatch) continue;

      int poleBars=p0.shift-p1.shift;
      int consolidationBars=p1.shift-p4.shift;
      if(poleBars<config.minPoleBars || poleBars>config.maxPoleBars ||
         consolidationBars<config.minConsolidationBars ||
         consolidationBars>config.maxConsolidationBars)
         continue;

      double referenceAtr=MathMax(p0.atr,MathMax(p1.atr,
         MathMax(p2.atr,MathMax(p3.atr,p4.atr))));
      if(!GS_ValidPositiveNumber(referenceAtr)) return false;
      double poleLength=bullish ? p1.price-p0.price : p0.price-p1.price;
      double poleStrengthAtr=poleLength/referenceAtr;
      if(!GS_ValidPositiveNumber(poleLength) || poleStrengthAtr<config.minPoleAtr)
         continue;

      double poleEfficiency=0.0;
      if(!GS_ContinuationPoleEfficiency(closedRates,rateIndex[0],rateIndex[1],
                                        bullish,poleEfficiency))
         return false;
      if(poleEfficiency<config.minPoleEfficiency) continue;

      double retracementDistance=bullish
         ? p1.price-MathMin(p2.price,p4.price)
         : MathMax(p2.price,p4.price)-p1.price;
      double retracementRatio=retracementDistance/poleLength;
      if(retracementRatio<config.minRetracementRatio ||
         retracementRatio>config.maxRetracementRatio)
         continue;

      double upperSlope=0.0,lowerSlope=0.0;
      double initialWidth=0.0,finalWidth=0.0;
      if(bullish)
      {
         upperSlope=(p3.price-p1.price)/(double)(rateIndex[3]-rateIndex[1]);
         lowerSlope=(p4.price-p2.price)/(double)(rateIndex[4]-rateIndex[2]);
         initialWidth=p1.price-p2.price;
         finalWidth=p3.price-p4.price;
      }
      else
      {
         upperSlope=(p4.price-p2.price)/(double)(rateIndex[4]-rateIndex[2]);
         lowerSlope=(p3.price-p1.price)/(double)(rateIndex[3]-rateIndex[1]);
         initialWidth=p2.price-p1.price;
         finalWidth=p4.price-p3.price;
      }
      if(!GS_ValidPositiveNumber(initialWidth) || !GS_ValidPositiveNumber(finalWidth))
         continue;

      double upperMove=MathAbs(upperSlope)*
         (double)(bullish ? rateIndex[3]-rateIndex[1] : rateIndex[4]-rateIndex[2]);
      double lowerMove=MathAbs(lowerSlope)*
         (double)(bullish ? rateIndex[4]-rateIndex[2] : rateIndex[3]-rateIndex[1]);
      if(upperMove<config.minGeometryMoveAtr*referenceAtr ||
         lowerMove<config.minGeometryMoveAtr*referenceAtr)
         continue;

      GoldScoutContinuationPatternType type=GOLDSCOUT_CONTINUATION_NONE;
      double geometryQuality=0.0;
      bool flagGeometry=bullish
         ? (upperSlope<0.0 && lowerSlope<0.0)
         : (upperSlope>0.0 && lowerSlope>0.0);
      if(flagGeometry)
      {
         double slopeRatio=MathMin(MathAbs(upperSlope),MathAbs(lowerSlope))/
            MathMax(MathAbs(upperSlope),MathAbs(lowerSlope));
         double widthRatio=finalWidth/initialWidth;
         if(slopeRatio>=config.minFlagParallelRatio &&
            widthRatio>=1.0-config.flagWidthTolerance &&
            widthRatio<=1.0+config.flagWidthTolerance)
         {
            double widthQuality=MathMax(0.0,MathMin(1.0,
               1.0-MathAbs(widthRatio-1.0)/config.flagWidthTolerance));
            geometryQuality=(slopeRatio+widthQuality)/2.0;
            type=bullish ? GOLDSCOUT_CONTINUATION_BULL_FLAG
                         : GOLDSCOUT_CONTINUATION_BEAR_FLAG;
         }
      }
      else if(upperSlope<0.0 && lowerSlope>0.0)
      {
         double widthRatio=finalWidth/initialWidth;
         double convergenceBalance=MathMin(upperMove,lowerMove)/
            MathMax(upperMove,lowerMove);
         if(widthRatio<1.0 && widthRatio<=config.maxPennantWidthRatio &&
            convergenceBalance>=config.minPennantConvergenceBalance)
         {
            geometryQuality=((1.0-widthRatio)+convergenceBalance)/2.0;
            type=bullish ? GOLDSCOUT_CONTINUATION_BULL_PENNANT
                         : GOLDSCOUT_CONTINUATION_BEAR_PENNANT;
         }
      }
      if(type==GOLDSCOUT_CONTINUATION_NONE) continue;

      pattern.detected=true;
      pattern.type=type;
      pattern.state=GOLDSCOUT_PATTERN_STATE_CANDIDATE;
      pattern.poleStart=p0;
      pattern.poleEnd=p1;
      pattern.correctionFirst=p2;
      pattern.correctionSecond=p3;
      pattern.correctionThird=p4;
      pattern.poleBars=poleBars;
      pattern.consolidationBars=consolidationBars;
      pattern.referenceAtr=referenceAtr;
      pattern.poleLength=poleLength;
      pattern.poleStrengthAtr=poleStrengthAtr;
      pattern.poleEfficiency=poleEfficiency;
      pattern.retracementRatio=retracementRatio;
      pattern.upperSlope=upperSlope;
      pattern.lowerSlope=lowerSlope;
      pattern.initialWidth=initialWidth;
      pattern.finalWidth=finalWidth;
      pattern.geometryQuality=geometryQuality;
      pattern.identity=StringFormat("%d:%I64d:%I64d:%I64d:%I64d:%I64d",
         (int)type,(long)p0.time,(long)p1.time,(long)p2.time,(long)p3.time,(long)p4.time);

      int upperAnchorIndex=bullish ? rateIndex[3] : rateIndex[4];
      int lowerAnchorIndex=bullish ? rateIndex[4] : rateIndex[3];
      double upperAnchorPrice=bullish ? p3.price : p4.price;
      double lowerAnchorPrice=bullish ? p4.price : p3.price;
      double breakoutBuffer=config.breakoutBufferAtr*referenceAtr;
      double invalidationBuffer=config.invalidationAtr*referenceAtr;
      for(int barIndex=rateIndex[4]+1;barIndex<ArraySize(closedRates);barIndex++)
      {
         pattern.barsAfterPattern++;
         if(pattern.barsAfterPattern>config.maxConfirmationBars)
         {
            pattern.state=GOLDSCOUT_PATTERN_STATE_EXPIRED;
            pattern.eventTime=closedRates[barIndex].time;
            break;
         }

         double upperBoundary=upperAnchorPrice+
            upperSlope*(double)(barIndex-upperAnchorIndex);
         double lowerBoundary=lowerAnchorPrice+
            lowerSlope*(double)(barIndex-lowerAnchorIndex);
         if(upperBoundary<=lowerBoundary)
         {
            pattern.state=GOLDSCOUT_PATTERN_STATE_EXPIRED;
            pattern.eventTime=closedRates[barIndex].time;
            break;
         }

         bool invalidated=bullish
            ? closedRates[barIndex].close<lowerBoundary-invalidationBuffer
            : closedRates[barIndex].close>upperBoundary+invalidationBuffer;
         if(invalidated)
         {
            pattern.state=GOLDSCOUT_PATTERN_STATE_INVALIDATED;
            pattern.eventTime=closedRates[barIndex].time;
            break;
         }

         bool confirmed=bullish
            ? closedRates[barIndex].close>upperBoundary+breakoutBuffer
            : closedRates[barIndex].close<lowerBoundary-breakoutBuffer;
         if(confirmed)
         {
            pattern.state=GOLDSCOUT_PATTERN_STATE_CONFIRMED;
            pattern.eventTime=closedRates[barIndex].time;
            pattern.breakoutStrengthAtr=bullish
               ? (closedRates[barIndex].close-upperBoundary)/referenceAtr
               : (lowerBoundary-closedRates[barIndex].close)/referenceAtr;
            pattern.volumeConfirmed=GS_ContinuationVolumeConfirmation(
               closedRates,barIndex,config);
            pattern.momentumConfirmed=GS_ContinuationMomentumConfirmation(
               closedRates[barIndex],bullish,referenceAtr,config);
            break;
         }
      }

      pattern.quality=GS_ContinuationPatternQuality(pattern,config);
      return true;
   }
   return true;
}

bool GS_FitThreePivotLine(const GoldScoutPivot &first,const int firstIndex,
                          const GoldScoutPivot &second,const int secondIndex,
                          const GoldScoutPivot &third,const int thirdIndex,
                          const double referenceAtr,const double maxLineFitAtr,
                          double &slope,double &intercept,double &fitQuality)
{
   slope=0.0;
   intercept=0.0;
   fitQuality=0.0;
   if(firstIndex<0 || secondIndex<=firstIndex || thirdIndex<=secondIndex ||
      !GS_ValidPositiveNumber(referenceAtr) || !GS_ValidPositiveNumber(maxLineFitAtr))
      return false;

   double meanX=((double)firstIndex+(double)secondIndex+(double)thirdIndex)/3.0;
   double meanY=(first.price+second.price+third.price)/3.0;
   double firstX=(double)firstIndex-meanX;
   double secondX=(double)secondIndex-meanX;
   double thirdX=(double)thirdIndex-meanX;
   double denominator=firstX*firstX+secondX*secondX+thirdX*thirdX;
   if(!GS_ValidPositiveNumber(denominator)) return false;

   slope=(firstX*(first.price-meanY)+secondX*(second.price-meanY)+
          thirdX*(third.price-meanY))/denominator;
   intercept=meanY-slope*meanX;
   if(!MathIsValidNumber(slope) || !MathIsValidNumber(intercept)) return false;

   double firstResidual=first.price-(intercept+slope*(double)firstIndex);
   double secondResidual=second.price-(intercept+slope*(double)secondIndex);
   double thirdResidual=third.price-(intercept+slope*(double)thirdIndex);
   double rms=MathSqrt((firstResidual*firstResidual+secondResidual*secondResidual+
                       thirdResidual*thirdResidual)/3.0);
   double maximumResidual=maxLineFitAtr*referenceAtr;
   if(!MathIsValidNumber(rms) || rms>maximumResidual) return false;
   fitQuality=MathMax(0.0,MathMin(1.0,1.0-rms/maximumResidual));
   return true;
}

double GS_ConvergenceSlopeQuality(const GoldScoutConvergencePatternType type,
                                  const double upperSlopeAtr,
                                  const double lowerSlopeAtr,
                                  const GoldScoutConvergencePatternConfig &config)
{
   double horizontalQuality=0.0;
   double directionalQuality=0.0;
   double separationQuality=MathMax(0.0,MathMin(1.0,
      (lowerSlopeAtr-upperSlopeAtr)/(2.0*config.minSlopeSeparationAtrPerBar)));

   if(type==GOLDSCOUT_CONVERGENCE_ASC_TRIANGLE)
   {
      horizontalQuality=MathMax(0.0,MathMin(1.0,
         1.0-MathAbs(upperSlopeAtr)/config.horizontalSlopeAtrPerBar));
      directionalQuality=MathMax(0.0,MathMin(1.0,
         lowerSlopeAtr/(2.0*config.minSlopeAtrPerBar)));
      return (horizontalQuality+directionalQuality+separationQuality)/3.0;
   }
   if(type==GOLDSCOUT_CONVERGENCE_DESC_TRIANGLE)
   {
      horizontalQuality=MathMax(0.0,MathMin(1.0,
         1.0-MathAbs(lowerSlopeAtr)/config.horizontalSlopeAtrPerBar));
      directionalQuality=MathMax(0.0,MathMin(1.0,
         -upperSlopeAtr/(2.0*config.minSlopeAtrPerBar)));
      return (horizontalQuality+directionalQuality+separationQuality)/3.0;
   }

   directionalQuality=MathMax(0.0,MathMin(1.0,
      MathMin(MathAbs(upperSlopeAtr),MathAbs(lowerSlopeAtr))/
      (2.0*config.minSlopeAtrPerBar)));
   return (directionalQuality+separationQuality)/2.0;
}

bool GS_ConvergenceVolumeConfirmation(const MqlRates &closedRates[],
                                      const int breakoutIndex,
                                      const GoldScoutConvergencePatternConfig &config)
{
   if(!config.useVolumeQuality || breakoutIndex<=0) return false;
   int first=(int)MathMax(0,breakoutIndex-config.volumeLookback);
   double total=0.0;
   int samples=0;
   for(int i=first;i<breakoutIndex;i++)
   {
      if(closedRates[i].tick_volume<=0) continue;
      total+=(double)closedRates[i].tick_volume;
      samples++;
   }
   if(samples<1 || closedRates[breakoutIndex].tick_volume<=0) return false;
   return (double)closedRates[breakoutIndex].tick_volume >=
      (total/(double)samples)*config.volumeMultiplier;
}

bool GS_ConvergenceMomentumConfirmation(const MqlRates &bar,const int direction,
                                        const double referenceAtr,
                                        const GoldScoutConvergencePatternConfig &config)
{
   if(!config.useMomentumQuality || direction==0 ||
      !GS_ValidPositiveNumber(referenceAtr)) return false;
   double directionalBody=direction>0 ? bar.close-bar.open : bar.open-bar.close;
   return directionalBody>=config.momentumBodyAtr*referenceAtr;
}

double GS_ConvergencePatternQuality(
   const GoldScoutConvergencePatternDiagnostic &pattern,
   const GoldScoutConvergencePatternConfig &config)
{
   if(!GS_ValidateConvergencePatternConfig(config) || !pattern.detected ||
      !GS_ValidPositiveNumber(pattern.referenceAtr))
      return 0.0;

   double idealDuration=((double)config.minPatternBars+(double)config.maxPatternBars)/2.0;
   double durationHalfRange=MathMax(0.5,
      ((double)config.maxPatternBars-(double)config.minPatternBars)/2.0);
   double durationQuality=MathMax(0.0,MathMin(1.0,
      1.0-MathAbs((double)pattern.durationBars-idealDuration)/durationHalfRange));
   double contractionQuality=MathMax(0.0,MathMin(1.0,1.0-pattern.widthRatio));
   double breakoutQuality=0.0;
   if(pattern.state==GOLDSCOUT_PATTERN_STATE_CONFIRMED)
      breakoutQuality=MathMax(0.0,MathMin(1.0,pattern.breakoutStrengthAtr));

   double quality=20.0*MathMax(0.0,MathMin(1.0,pattern.lineFitQuality))+
      15.0*MathMax(0.0,MathMin(1.0,pattern.slopeQuality))+
      15.0*MathMax(0.0,MathMin(1.0,pattern.convergenceQuality))+
      10.0*durationQuality+15.0*contractionQuality+
      10.0*MathMax(0.0,MathMin(1.0,pattern.apexPositionQuality))+
      10.0*breakoutQuality;
   if(pattern.volumeConfirmed) quality+=2.5;
   if(pattern.momentumConfirmed) quality+=2.5;
   return MathMax(0.0,MathMin(100.0,quality));
}

// Detect one newest triangle or wedge identity from six adjacent confirmed,
// alternating pivots (three resistance and three support touches). Geometry and
// every state transition use closedRates only. The first terminal close is
// immutable for the identity, so later candles cannot repaint the result.
bool GS_DetectLatestConvergencePattern(
   const GoldScoutPivot &confirmedPivots[],
   const MqlRates &closedRates[],
   const int newestClosedShift,
   const GoldScoutConvergencePatternConfig &config,
   const double minimumPriceStep,
   GoldScoutConvergencePatternDiagnostic &pattern)
{
   GS_ClearConvergencePatternDiagnostic(pattern);
   if(newestClosedShift<1 || !GS_ValidateConvergencePatternConfig(config) ||
      !GS_ValidPositiveNumber(minimumPriceStep) || ArraySize(closedRates)<6 ||
      !GS_ClosedRatesValid(closedRates))
      return false;

   GoldScoutPivot alternating[];
   if(!GS_NormalizeAlternatingPivots(confirmedPivots,alternating)) return false;

   for(int start=ArraySize(alternating)-6;start>=0;start--)
   {
      GoldScoutPivot sequence[6];
      int rateIndex[6];
      bool sequenceValid=true;
      for(int position=0;position<6;position++)
      {
         sequence[position]=alternating[start+position];
         rateIndex[position]=GS_FindClosedRateByTime(closedRates,sequence[position].time);
         if(rateIndex[position]<0 || (position>0 && rateIndex[position]<=rateIndex[position-1]))
         {
            sequenceValid=false;
            break;
         }
         int expectedShift=newestClosedShift+
            (ArraySize(closedRates)-1-rateIndex[position]);
         if(sequence[position].shift!=expectedShift)
         {
            sequenceValid=false;
            break;
         }
      }
      if(!sequenceValid) continue;

      int durationBars=rateIndex[5]-rateIndex[0];
      if(durationBars<config.minPatternBars || durationBars>config.maxPatternBars)
         continue;

      GoldScoutPivot upper[3],lower[3];
      int upperIndex[3],lowerIndex[3];
      int upperCount=0,lowerCount=0;
      double referenceAtr=0.0;
      for(int position=0;position<6;position++)
      {
         referenceAtr=MathMax(referenceAtr,sequence[position].atr);
         if(sequence[position].type==GOLDSCOUT_PIVOT_HIGH && upperCount<3)
         {
            upper[upperCount]=sequence[position];
            upperIndex[upperCount]=rateIndex[position];
            upperCount++;
         }
         else if(sequence[position].type==GOLDSCOUT_PIVOT_LOW && lowerCount<3)
         {
            lower[lowerCount]=sequence[position];
            lowerIndex[lowerCount]=rateIndex[position];
            lowerCount++;
         }
      }
      if(upperCount!=3 || lowerCount!=3 || !GS_ValidPositiveNumber(referenceAtr))
         continue;

      double upperSlope=0.0,upperIntercept=0.0,upperFit=0.0;
      double lowerSlope=0.0,lowerIntercept=0.0,lowerFit=0.0;
      if(!GS_FitThreePivotLine(upper[0],upperIndex[0],upper[1],upperIndex[1],
                              upper[2],upperIndex[2],referenceAtr,
                              config.maxLineFitAtr,upperSlope,upperIntercept,upperFit) ||
         !GS_FitThreePivotLine(lower[0],lowerIndex[0],lower[1],lowerIndex[1],
                              lower[2],lowerIndex[2],referenceAtr,
                              config.maxLineFitAtr,lowerSlope,lowerIntercept,lowerFit))
         continue;

      double upperSlopeAtr=upperSlope/referenceAtr;
      double lowerSlopeAtr=lowerSlope/referenceAtr;
      double slopeSeparationAtr=lowerSlopeAtr-upperSlopeAtr;
      if(slopeSeparationAtr<config.minSlopeSeparationAtrPerBar) continue;

      bool upperHorizontal=MathAbs(upperSlopeAtr)<=config.horizontalSlopeAtrPerBar;
      bool lowerHorizontal=MathAbs(lowerSlopeAtr)<=config.horizontalSlopeAtrPerBar;
      bool upperRising=upperSlopeAtr>=config.minSlopeAtrPerBar;
      bool lowerRising=lowerSlopeAtr>=config.minSlopeAtrPerBar;
      bool upperFalling=upperSlopeAtr<=-config.minSlopeAtrPerBar;
      bool lowerFalling=lowerSlopeAtr<=-config.minSlopeAtrPerBar;
      GoldScoutConvergencePatternType type=GOLDSCOUT_CONVERGENCE_NONE;
      if(upperHorizontal && lowerRising)
         type=GOLDSCOUT_CONVERGENCE_ASC_TRIANGLE;
      else if(lowerHorizontal && upperFalling)
         type=GOLDSCOUT_CONVERGENCE_DESC_TRIANGLE;
      else if(upperFalling && lowerRising)
         type=GOLDSCOUT_CONVERGENCE_SYMM_TRIANGLE;
      else if(upperRising && lowerRising && lowerSlopeAtr>upperSlopeAtr)
         type=GOLDSCOUT_CONVERGENCE_RISING_WEDGE;
      else if(upperFalling && lowerFalling && upperSlopeAtr<lowerSlopeAtr)
         type=GOLDSCOUT_CONVERGENCE_FALLING_WEDGE;
      if(type==GOLDSCOUT_CONVERGENCE_NONE) continue;

      double initialUpper=upperIntercept+upperSlope*(double)rateIndex[0];
      double initialLower=lowerIntercept+lowerSlope*(double)rateIndex[0];
      double finalUpper=upperIntercept+upperSlope*(double)rateIndex[5];
      double finalLower=lowerIntercept+lowerSlope*(double)rateIndex[5];
      double initialWidth=initialUpper-initialLower;
      double finalWidth=finalUpper-finalLower;
      if(!GS_ValidPositiveNumber(initialWidth) || !GS_ValidPositiveNumber(finalWidth))
         continue;
      double widthRatio=finalWidth/initialWidth;
      if(!GS_ValidPositiveNumber(widthRatio) || widthRatio>config.maxWidthRatio)
         continue;

      double slopeDifference=lowerSlope-upperSlope;
      if(!GS_ValidPositiveNumber(slopeDifference)) continue;
      double apexIndex=(upperIntercept-lowerIntercept)/slopeDifference;
      double apexDistance=apexIndex-(double)rateIndex[5];
      if(!MathIsValidNumber(apexIndex) ||
         apexDistance<(double)config.minBarsBeforeApex ||
         apexDistance>(double)durationBars*config.maxApexDistanceRatio)
         continue;

      pattern.detected=true;
      pattern.type=type;
      pattern.state=GOLDSCOUT_PATTERN_STATE_CANDIDATE;
      pattern.firstUpper=upper[0];
      pattern.secondUpper=upper[1];
      pattern.thirdUpper=upper[2];
      pattern.firstLower=lower[0];
      pattern.secondLower=lower[1];
      pattern.thirdLower=lower[2];
      pattern.durationBars=durationBars;
      pattern.referenceAtr=referenceAtr;
      pattern.upperSlope=upperSlope;
      pattern.lowerSlope=lowerSlope;
      pattern.upperIntercept=upperIntercept;
      pattern.lowerIntercept=lowerIntercept;
      pattern.initialWidth=initialWidth;
      pattern.finalWidth=finalWidth;
      pattern.widthRatio=widthRatio;
      pattern.lineFitQuality=(upperFit+lowerFit)/2.0;
      pattern.slopeQuality=GS_ConvergenceSlopeQuality(
         type,upperSlopeAtr,lowerSlopeAtr,config);
      pattern.convergenceQuality=MathMax(0.0,MathMin(1.0,
         slopeSeparationAtr/(2.0*config.minSlopeSeparationAtrPerBar)));
      pattern.apexIndex=apexIndex;
      double apexDistanceRatio=apexDistance/(double)durationBars;
      pattern.apexPositionQuality=MathMax(0.0,MathMin(1.0,
         1.0-MathAbs(apexDistanceRatio-0.75)/0.75));
      pattern.identity=StringFormat(
         "%d:%I64d:%I64d:%I64d:%I64d:%I64d:%I64d",(int)type,
         (long)sequence[0].time,(long)sequence[1].time,(long)sequence[2].time,
         (long)sequence[3].time,(long)sequence[4].time,(long)sequence[5].time);

      double breakoutBuffer=MathMax(minimumPriceStep,
         config.breakoutBufferAtr*referenceAtr);
      for(int barIndex=rateIndex[5]+1;barIndex<ArraySize(closedRates);barIndex++)
      {
         pattern.barsAfterPattern++;
         double barsBeforeApex=apexIndex-(double)barIndex;
         if(pattern.barsAfterPattern>config.maxConfirmationBars ||
            (double)barIndex>=apexIndex ||
            barsBeforeApex<(double)config.minBreakoutBarsBeforeApex)
         {
            pattern.state=GOLDSCOUT_PATTERN_STATE_EXPIRED;
            pattern.eventTime=closedRates[barIndex].time;
            break;
         }

         double upperBoundary=upperIntercept+upperSlope*(double)barIndex;
         double lowerBoundary=lowerIntercept+lowerSlope*(double)barIndex;
         if(!GS_ValidPositiveNumber(upperBoundary) ||
            !GS_ValidPositiveNumber(lowerBoundary) || upperBoundary<=lowerBoundary)
         {
            pattern.state=GOLDSCOUT_PATTERN_STATE_EXPIRED;
            pattern.eventTime=closedRates[barIndex].time;
            break;
         }

         bool longBreak=closedRates[barIndex].close>upperBoundary+breakoutBuffer;
         bool shortBreak=closedRates[barIndex].close<lowerBoundary-breakoutBuffer;
         bool primaryLong=type==GOLDSCOUT_CONVERGENCE_ASC_TRIANGLE;
         bool primaryShort=type==GOLDSCOUT_CONVERGENCE_DESC_TRIANGLE;
         if((primaryLong && shortBreak) || (primaryShort && longBreak))
         {
            pattern.state=GOLDSCOUT_PATTERN_STATE_INVALIDATED;
            pattern.eventTime=closedRates[barIndex].time;
            pattern.breakoutDirection=shortBreak ? -1 : 1;
            break;
         }

         bool confirmed=(primaryLong && longBreak) || (primaryShort && shortBreak) ||
            ((!primaryLong && !primaryShort) && (longBreak || shortBreak));
         if(confirmed)
         {
            pattern.state=GOLDSCOUT_PATTERN_STATE_CONFIRMED;
            pattern.eventTime=closedRates[barIndex].time;
            pattern.breakoutDirection=longBreak ? 1 : -1;
            pattern.breakoutStrengthAtr=longBreak
               ? (closedRates[barIndex].close-upperBoundary)/referenceAtr
               : (lowerBoundary-closedRates[barIndex].close)/referenceAtr;
            pattern.volumeConfirmed=GS_ConvergenceVolumeConfirmation(
               closedRates,barIndex,config);
            pattern.momentumConfirmed=GS_ConvergenceMomentumConfirmation(
               closedRates[barIndex],pattern.breakoutDirection,referenceAtr,config);
            double progress=((double)barIndex-(double)rateIndex[5])/
               (apexIndex-(double)rateIndex[5]);
            pattern.apexPositionQuality=MathMax(0.0,MathMin(1.0,
               1.0-MathAbs(progress-0.65)/0.65));
            break;
         }
      }

      pattern.quality=GS_ConvergencePatternQuality(pattern,config);
      return true;
   }
   return true;
}

bool GS_HeadShouldersVolumeConfirmation(
   const MqlRates &closedRates[],const int breakoutIndex,
   const GoldScoutHeadShouldersPatternConfig &config)
{
   if(!config.useVolumeQuality || breakoutIndex<=0) return false;
   int first=(int)MathMax(0,breakoutIndex-config.volumeLookback);
   double total=0.0;
   int samples=0;
   for(int i=first;i<breakoutIndex;i++)
   {
      if(closedRates[i].tick_volume<=0) continue;
      total+=(double)closedRates[i].tick_volume;
      samples++;
   }
   if(samples<1 || closedRates[breakoutIndex].tick_volume<=0) return false;
   return (double)closedRates[breakoutIndex].tick_volume >=
      (total/(double)samples)*config.volumeMultiplier;
}

bool GS_HeadShouldersMomentumConfirmation(
   const MqlRates &bar,const int direction,const double referenceAtr,
   const GoldScoutHeadShouldersPatternConfig &config)
{
   if(!config.useMomentumQuality || direction==0 ||
      !GS_ValidPositiveNumber(referenceAtr)) return false;
   double directionalBody=direction>0 ? bar.close-bar.open : bar.open-bar.close;
   return directionalBody>=config.momentumBodyAtr*referenceAtr;
}

double GS_HeadShouldersPatternQuality(
   const GoldScoutHeadShouldersPatternDiagnostic &pattern,
   const GoldScoutHeadShouldersPatternConfig &config)
{
   if(!GS_ValidateHeadShouldersPatternConfig(config) || !pattern.detected ||
      !GS_ValidPositiveNumber(pattern.referenceAtr) ||
      !GS_ValidPositiveNumber(pattern.shoulderTolerance))
      return 0.0;

   double shoulderSymmetry=MathMax(0.0,MathMin(1.0,
      1.0-pattern.shoulderDifference/pattern.shoulderTolerance));
   double minimumProminence=config.minHeadProminenceAtr*pattern.referenceAtr;
   double prominenceQuality=MathMax(0.0,MathMin(1.0,
      pattern.headProminence/(2.0*minimumProminence)));
   double necklineQuality=MathMax(0.0,MathMin(1.0,
      1.0-MathAbs(pattern.necklineSlopeAtrPerBar)/
      config.maxNecklineSlopeAtrPerBar));
   double minimumDepth=config.minDepthAtr*pattern.referenceAtr;
   double depthQuality=MathMax(0.0,MathMin(1.0,
      pattern.depth/(2.0*minimumDepth)));
   double breakoutQuality=0.0;
   if(pattern.state==GOLDSCOUT_PATTERN_STATE_CONFIRMED)
      breakoutQuality=MathMax(0.0,MathMin(1.0,pattern.breakoutStrengthAtr));

   double quality=20.0*shoulderSymmetry+20.0*prominenceQuality+
      15.0*MathMax(0.0,MathMin(1.0,pattern.temporalBalance))+
      15.0*necklineQuality+15.0*depthQuality+10.0*breakoutQuality;
   if(pattern.volumeConfirmed) quality+=2.5;
   if(pattern.momentumConfirmed) quality+=2.5;
   return MathMax(0.0,MathMin(100.0,quality));
}

// Detect one newest HCH identity from five adjacent confirmed alternating
// pivots. closedRates is chronological and excludes the open H1 candle. The
// first confirmation, invalidation or expiry is terminal for the identity.
bool GS_DetectLatestHeadShouldersPattern(
   const GoldScoutPivot &confirmedPivots[],
   const MqlRates &closedRates[],
   const int newestClosedShift,
   const GoldScoutHeadShouldersPatternConfig &config,
   const double minimumPriceStep,
   GoldScoutHeadShouldersPatternDiagnostic &pattern)
{
   GS_ClearHeadShouldersPatternDiagnostic(pattern);
   if(newestClosedShift<1 || !GS_ValidateHeadShouldersPatternConfig(config) ||
      !GS_ValidPositiveNumber(minimumPriceStep) || ArraySize(closedRates)<5 ||
      !GS_ClosedRatesValid(closedRates))
      return false;

   GoldScoutPivot alternating[];
   if(!GS_NormalizeAlternatingPivots(confirmedPivots,alternating)) return false;

   for(int start=ArraySize(alternating)-5;start>=0;start--)
   {
      GoldScoutPivot sequence[5];
      int rateIndex[5];
      bool sequenceValid=true;
      for(int position=0;position<5;position++)
      {
         sequence[position]=alternating[start+position];
         rateIndex[position]=GS_FindClosedRateByTime(closedRates,sequence[position].time);
         if(rateIndex[position]<0 || (position>0 && rateIndex[position]<=rateIndex[position-1]))
         {
            sequenceValid=false;
            break;
         }
         int expectedShift=newestClosedShift+
            (ArraySize(closedRates)-1-rateIndex[position]);
         if(sequence[position].shift!=expectedShift)
         {
            sequenceValid=false;
            break;
         }
      }
      if(!sequenceValid) continue;

      bool bearish=sequence[0].type==GOLDSCOUT_PIVOT_HIGH &&
         sequence[1].type==GOLDSCOUT_PIVOT_LOW &&
         sequence[2].type==GOLDSCOUT_PIVOT_HIGH &&
         sequence[3].type==GOLDSCOUT_PIVOT_LOW &&
         sequence[4].type==GOLDSCOUT_PIVOT_HIGH;
      bool bullish=sequence[0].type==GOLDSCOUT_PIVOT_LOW &&
         sequence[1].type==GOLDSCOUT_PIVOT_HIGH &&
         sequence[2].type==GOLDSCOUT_PIVOT_LOW &&
         sequence[3].type==GOLDSCOUT_PIVOT_HIGH &&
         sequence[4].type==GOLDSCOUT_PIVOT_LOW;
      if(!bearish && !bullish) continue;

      int firstLeg=rateIndex[1]-rateIndex[0];
      int secondLeg=rateIndex[2]-rateIndex[1];
      int thirdLeg=rateIndex[3]-rateIndex[2];
      int fourthLeg=rateIndex[4]-rateIndex[3];
      if(firstLeg<config.minPivotBars || secondLeg<config.minPivotBars ||
         thirdLeg<config.minPivotBars || fourthLeg<config.minPivotBars ||
         firstLeg>config.maxPivotBars || secondLeg>config.maxPivotBars ||
         thirdLeg>config.maxPivotBars || fourthLeg>config.maxPivotBars)
         continue;

      int leftSpan=rateIndex[2]-rateIndex[0];
      int rightSpan=rateIndex[4]-rateIndex[2];
      double temporalBalance=(double)MathMin(leftSpan,rightSpan)/
         (double)MathMax(leftSpan,rightSpan);
      if(temporalBalance<config.minTemporalBalance) continue;

      double referenceAtr=0.0;
      for(int position=0;position<5;position++)
         referenceAtr=MathMax(referenceAtr,sequence[position].atr);
      if(!GS_ValidPositiveNumber(referenceAtr)) continue;

      double shoulderTolerance=0.0;
      if(!GS_ATRTolerance(sequence[0].atr,sequence[4].atr,
                          config.shoulderToleranceAtr,minimumPriceStep,
                          shoulderTolerance))
         return false;
      double shoulderDifference=MathAbs(sequence[0].price-sequence[4].price);
      if(shoulderDifference>shoulderTolerance) continue;

      double headProminence=bearish
         ? MathMin(sequence[2].price-sequence[0].price,
                   sequence[2].price-sequence[4].price)
         : MathMin(sequence[0].price-sequence[2].price,
                   sequence[4].price-sequence[2].price);
      if(headProminence<config.minHeadProminenceAtr*referenceAtr) continue;

      double necklineSlope=(sequence[3].price-sequence[1].price)/
         (double)(rateIndex[3]-rateIndex[1]);
      double necklineSlopeAtrPerBar=necklineSlope/referenceAtr;
      if(!MathIsValidNumber(necklineSlope) ||
         MathAbs(necklineSlopeAtrPerBar)>config.maxNecklineSlopeAtrPerBar)
         continue;
      double necklineIntercept=sequence[1].price-
         necklineSlope*(double)rateIndex[1];
      double necklineAtHead=necklineIntercept+
         necklineSlope*(double)rateIndex[2];
      double depth=bearish ? sequence[2].price-necklineAtHead
                           : necklineAtHead-sequence[2].price;
      if(depth<config.minDepthAtr*referenceAtr) continue;

      double necklineAtLeft=necklineIntercept+
         necklineSlope*(double)rateIndex[0];
      double necklineAtRight=necklineIntercept+
         necklineSlope*(double)rateIndex[4];
      bool shouldersValid=bearish
         ? (sequence[0].price>necklineAtLeft+minimumPriceStep &&
            sequence[4].price>necklineAtRight+minimumPriceStep)
         : (sequence[0].price<necklineAtLeft-minimumPriceStep &&
            sequence[4].price<necklineAtRight-minimumPriceStep);
      if(!shouldersValid) continue;

      GoldScoutHeadShouldersPatternType type=bearish
         ? GOLDSCOUT_HEAD_SHOULDERS_HCH
         : GOLDSCOUT_HEAD_SHOULDERS_INVERTED;
      pattern.detected=true;
      pattern.type=type;
      pattern.state=GOLDSCOUT_PATTERN_STATE_CANDIDATE;
      pattern.leftShoulder=sequence[0];
      pattern.firstNeckline=sequence[1];
      pattern.head=sequence[2];
      pattern.secondNeckline=sequence[3];
      pattern.rightShoulder=sequence[4];
      pattern.firstLegBars=firstLeg;
      pattern.secondLegBars=secondLeg;
      pattern.thirdLegBars=thirdLeg;
      pattern.fourthLegBars=fourthLeg;
      pattern.leftSpanBars=leftSpan;
      pattern.rightSpanBars=rightSpan;
      pattern.referenceAtr=referenceAtr;
      pattern.shoulderTolerance=shoulderTolerance;
      pattern.shoulderDifference=shoulderDifference;
      pattern.headProminence=headProminence;
      pattern.depth=depth;
      pattern.temporalBalance=temporalBalance;
      pattern.necklineSlope=necklineSlope;
      pattern.necklineSlopeAtrPerBar=necklineSlopeAtrPerBar;
      pattern.necklineAtHead=necklineAtHead;
      pattern.identity=StringFormat("%d:%I64d:%I64d:%I64d:%I64d:%I64d",
         (int)type,(long)sequence[0].time,(long)sequence[1].time,
         (long)sequence[2].time,(long)sequence[3].time,(long)sequence[4].time);

      double breakoutBuffer=MathMax(minimumPriceStep,
         config.breakoutBufferAtr*referenceAtr);
      double invalidationBuffer=MathMax(minimumPriceStep,
         config.invalidationAtr*referenceAtr);
      for(int barIndex=rateIndex[4]+1;barIndex<ArraySize(closedRates);barIndex++)
      {
         pattern.barsAfterPattern++;
         if(pattern.barsAfterPattern>config.maxConfirmationBars)
         {
            pattern.state=GOLDSCOUT_PATTERN_STATE_EXPIRED;
            pattern.eventTime=closedRates[barIndex].time;
            break;
         }

         double neckline=necklineIntercept+necklineSlope*(double)barIndex;
         bool invalidated=bearish
            ? closedRates[barIndex].close>sequence[2].price+invalidationBuffer
            : closedRates[barIndex].close<sequence[2].price-invalidationBuffer;
         if(invalidated)
         {
            pattern.state=GOLDSCOUT_PATTERN_STATE_INVALIDATED;
            pattern.eventTime=closedRates[barIndex].time;
            break;
         }

         bool confirmed=bearish
            ? closedRates[barIndex].close<neckline-breakoutBuffer
            : closedRates[barIndex].close>neckline+breakoutBuffer;
         if(confirmed)
         {
            pattern.state=GOLDSCOUT_PATTERN_STATE_CONFIRMED;
            pattern.eventTime=closedRates[barIndex].time;
            pattern.breakoutDirection=bearish ? -1 : 1;
            pattern.breakoutStrengthAtr=bearish
               ? (neckline-closedRates[barIndex].close)/referenceAtr
               : (closedRates[barIndex].close-neckline)/referenceAtr;
            pattern.volumeConfirmed=GS_HeadShouldersVolumeConfirmation(
               closedRates,barIndex,config);
            pattern.momentumConfirmed=GS_HeadShouldersMomentumConfirmation(
               closedRates[barIndex],pattern.breakoutDirection,referenceAtr,config);
            break;
         }
      }

      pattern.quality=GS_HeadShouldersPatternQuality(pattern,config);
      return true;
   }
   return true;
}

// closedRates and closedAtr must be ordered from oldest to newest and must not
// contain the currently open candle. newestClosedShift therefore cannot be 0.
bool GS_DetectConfirmedPivots(const MqlRates &closedRates[],
                              const double &closedAtr[],
                              const int newestClosedShift,
                              const GoldScoutPivotConfig &config,
                              GoldScoutPivot &pivots[])
{
   GS_ClearPivots(pivots);
   if(newestClosedShift<1 || !GS_ValidatePivotConfig(config)) return false;

   int count=ArraySize(closedRates);
   if(count!=ArraySize(closedAtr)) return false;
   for(int i=0;i<count;i++)
   {
      if(closedRates[i].time<=0 ||
         !MathIsValidNumber(closedRates[i].high) ||
         !MathIsValidNumber(closedRates[i].low) ||
         closedRates[i].high<closedRates[i].low ||
         !GS_ValidPositiveNumber(closedAtr[i]))
         return false;
      if(i>0 && closedRates[i].time<=closedRates[i-1].time) return false;
   }

   if(count<config.leftBars+config.rightBars+1) return true;

   int lastAcceptedIndex=-1;
   for(int i=config.leftBars;i<count-config.rightBars;i++)
   {
      bool isHigh=true;
      bool isLow=true;
      double neighbourHigh=closedRates[i-config.leftBars].high;
      double neighbourLow=closedRates[i-config.leftBars].low;

      for(int j=i-config.leftBars;j<=i+config.rightBars;j++)
      {
         if(j==i) continue;
         if(closedRates[i].high<=closedRates[j].high) isHigh=false;
         if(closedRates[i].low>=closedRates[j].low) isLow=false;
         neighbourHigh=MathMax(neighbourHigh,closedRates[j].high);
         neighbourLow=MathMin(neighbourLow,closedRates[j].low);
      }

      double requiredProminence=config.minProminenceAtr*closedAtr[i];
      if(isHigh && closedRates[i].high-neighbourHigh+1e-12<requiredProminence) isHigh=false;
      if(isLow && neighbourLow-closedRates[i].low+1e-12<requiredProminence) isLow=false;

      // An outside bar that qualifies as both directions is ambiguous. Keep the
      // detector fail-closed by emitting neither pivot.
      if(isHigh==isLow) continue;
      if(lastAcceptedIndex>=0 && i-lastAcceptedIndex<config.minBarsBetween) continue;

      int size=ArraySize(pivots);
      if(ArrayResize(pivots,size+1)!=size+1)
      {
         GS_ClearPivots(pivots);
         return false;
      }

      pivots[size].type=isHigh?GOLDSCOUT_PIVOT_HIGH:GOLDSCOUT_PIVOT_LOW;
      pivots[size].price=isHigh?closedRates[i].high:closedRates[i].low;
      pivots[size].shift=newestClosedShift+(count-1-i);
      pivots[size].time=closedRates[i].time;
      pivots[size].atr=closedAtr[i];
      pivots[size].confirmed=true;
      lastAcceptedIndex=i;
   }
   return true;
}

// Terminal adapter: start_pos=1 explicitly excludes the open candle. CopyRates
// and CopyBuffer populate physical memory oldest-to-newest for this detector.
bool GS_LoadConfirmedPivots(const string symbol,
                            const ENUM_TIMEFRAMES timeframe,
                            const int atrHandle,
                            const int barsToLoad,
                            const GoldScoutPivotConfig &config,
                            GoldScoutPivot &pivots[])
{
   GS_ClearPivots(pivots);
   if(symbol=="" || atrHandle==INVALID_HANDLE ||
      barsToLoad<config.leftBars+config.rightBars+1 ||
      !GS_ValidatePivotConfig(config))
      return false;

   MqlRates closedRates[];
   double closedAtr[];
   ArraySetAsSeries(closedRates,false);
   ArraySetAsSeries(closedAtr,false);

   int copied=CopyRates(symbol,timeframe,1,barsToLoad,closedRates);
   if(copied<config.leftBars+config.rightBars+1) return false;
   if(ArrayResize(closedAtr,copied)!=copied) return false;
   if(CopyBuffer(atrHandle,0,1,copied,closedAtr)!=copied) return false;

   return GS_DetectConfirmedPivots(closedRates,closedAtr,1,config,pivots);
}

// Terminal adapter for diagnostics. start_pos=1 guarantees that neckline
// confirmation, invalidation and expiry are evaluated with closed candles only.
bool GS_LoadLatestPatternDiagnostic(const string symbol,
                                    const ENUM_TIMEFRAMES timeframe,
                                    const int barsToLoad,
                                    const GoldScoutPivot &confirmedPivots[],
                                    const double minimumPriceStep,
                                    const GoldScoutPatternConfig &config,
                                    GoldScoutPatternDiagnostic &pattern)
{
   GS_ClearPatternDiagnostic(pattern);
   if(symbol=="" || barsToLoad<3 || !GS_ValidatePatternConfig(config) ||
      !GS_ValidPositiveNumber(minimumPriceStep))
      return false;

   MqlRates closedRates[];
   ArraySetAsSeries(closedRates,false);
   int copied=CopyRates(symbol,timeframe,1,barsToLoad,closedRates);
   if(copied<3) return false;
   return GS_DetectLatestConfirmedPattern(confirmedPivots,closedRates,1,
      config,minimumPriceStep,pattern);
}

// Closed-bar terminal adapter for diagnostic flags and pennants.
bool GS_LoadLatestContinuationPatternDiagnostic(
   const string symbol,
   const ENUM_TIMEFRAMES timeframe,
   const int barsToLoad,
   const GoldScoutPivot &confirmedPivots[],
   const double minimumPriceStep,
   const GoldScoutContinuationPatternConfig &config,
   GoldScoutContinuationPatternDiagnostic &pattern)
{
   GS_ClearContinuationPatternDiagnostic(pattern);
   if(symbol=="" || barsToLoad<5 ||
      !GS_ValidateContinuationPatternConfig(config) ||
      !GS_ValidPositiveNumber(minimumPriceStep))
      return false;

   MqlRates closedRates[];
   ArraySetAsSeries(closedRates,false);
   int copied=CopyRates(symbol,timeframe,1,barsToLoad,closedRates);
   if(copied<5) return false;
   return GS_DetectLatestContinuationPattern(confirmedPivots,closedRates,1,
      config,minimumPriceStep,pattern);
}

// Closed-bar terminal adapter for diagnostic triangles and wedges.
bool GS_LoadLatestConvergencePatternDiagnostic(
   const string symbol,
   const ENUM_TIMEFRAMES timeframe,
   const int barsToLoad,
   const GoldScoutPivot &confirmedPivots[],
   const double minimumPriceStep,
   const GoldScoutConvergencePatternConfig &config,
   GoldScoutConvergencePatternDiagnostic &pattern)
{
   GS_ClearConvergencePatternDiagnostic(pattern);
   if(symbol=="" || barsToLoad<6 ||
      !GS_ValidateConvergencePatternConfig(config) ||
      !GS_ValidPositiveNumber(minimumPriceStep))
      return false;

   MqlRates closedRates[];
   ArraySetAsSeries(closedRates,false);
   int copied=CopyRates(symbol,timeframe,1,barsToLoad,closedRates);
   if(copied<6) return false;
   return GS_DetectLatestConvergencePattern(confirmedPivots,closedRates,1,
      config,minimumPriceStep,pattern);
}

// Closed-bar terminal adapter for diagnostic HCH and inverted HCH patterns.
bool GS_LoadLatestHeadShouldersPatternDiagnostic(
   const string symbol,
   const ENUM_TIMEFRAMES timeframe,
   const int barsToLoad,
   const GoldScoutPivot &confirmedPivots[],
   const double minimumPriceStep,
   const GoldScoutHeadShouldersPatternConfig &config,
   GoldScoutHeadShouldersPatternDiagnostic &pattern)
{
   GS_ClearHeadShouldersPatternDiagnostic(pattern);
   if(symbol=="" || barsToLoad<5 ||
      !GS_ValidateHeadShouldersPatternConfig(config) ||
      !GS_ValidPositiveNumber(minimumPriceStep))
      return false;

   MqlRates closedRates[];
   ArraySetAsSeries(closedRates,false);
   int copied=CopyRates(symbol,timeframe,1,barsToLoad,closedRates);
   if(copied<5) return false;
   return GS_DetectLatestHeadShouldersPattern(confirmedPivots,closedRates,1,
      config,minimumPriceStep,pattern);
}

#endif
