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

const int GOLDSCOUT_MAX_STRUCTURAL_BUCKET_POINTS=25;

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

// The structural bucket is directional and deliberately capped. Pullback adds
// only confirmation value on top of an already-confirmed structure. Momentum
// is not counted separately when it represents the same impulse as breakout.
int GS_StructuralBucketPoints(const GoldScoutStructureState state,
                              const int direction,
                              const bool pullback,
                              const bool breakout,
                              const bool momentum)
{
   if((direction>0 && state!=GOLDSCOUT_STRUCTURE_BULLISH) ||
      (direction<0 && state!=GOLDSCOUT_STRUCTURE_BEARISH) ||
      direction==0)
      return 0;

   int points=15;
   if(pullback) points+=5;
   if(breakout) points+=10;
   else if(momentum) points+=5;
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

#endif
