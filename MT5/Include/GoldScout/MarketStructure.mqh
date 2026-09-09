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

string GS_StructureStateName(const GoldScoutStructureState state)
{
   if(state==GOLDSCOUT_STRUCTURE_BULLISH) return "ALCISTA";
   if(state==GOLDSCOUT_STRUCTURE_BEARISH) return "BAJISTA";
   if(state==GOLDSCOUT_STRUCTURE_NEUTRAL) return "NEUTRA / INDETERMINADA";
   return "INSUFICIENTE";
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

#endif
