#ifndef GOLDSCOUT_CONTEXT_SCORING_MQH
#define GOLDSCOUT_CONTEXT_SCORING_MQH

// Soft session/news context. It never replaces H4/H1 direction or M15 timing.
const int GOLDSCOUT_MAX_CONTEXT_POINTS=4;
const int GOLDSCOUT_MAX_NEWS_CONTEXT_POINTS=2;

struct GoldScoutSessionContext
{
   bool     available;
   string   name;
   string   phase;
   string   volatility;
   string   activeCenters;
   int      asiaActiveCenters;
   bool     londonActive;
   bool     newYorkActive;
   bool     openingWindow;
   int      points;
   datetime referenceOpenTime;
};

void GS_ClearSessionContext(GoldScoutSessionContext &context)
{
   context.available=false;
   context.name="NO DISPONIBLE";
   context.phase="-";
   context.volatility="-";
   context.activeCenters="";
   context.asiaActiveCenters=0;
   context.londonActive=false;
   context.newYorkActive=false;
   context.openingWindow=false;
   context.points=0;
   context.referenceOpenTime=0;
}

int GS_ContextWeekday(const int year,const int month,const int day)
{
   MqlDateTime value;
   ZeroMemory(value);
   value.year=year;
   value.mon=month;
   value.day=day;
   value.hour=12;
   datetime stamp=StructToTime(value);
   MqlDateTime normalized;
   ZeroMemory(normalized);
   if(stamp<=0 || !TimeToStruct(stamp,normalized)) return -1;
   return normalized.day_of_week;
}

int GS_NthSunday(const int year,const int month,const int occurrence)
{
   if(occurrence<1) return 0;
   int firstWeekday=GS_ContextWeekday(year,month,1);
   if(firstWeekday<0) return 0;
   int firstSunday=1+((7-firstWeekday)%7);
   return firstSunday+(occurrence-1)*7;
}

int GS_LastSunday(const int year,const int month,const int daysInMonth)
{
   int lastWeekday=GS_ContextWeekday(year,month,daysInMonth);
   if(lastWeekday<0) return 0;
   return daysInMonth-lastWeekday;
}

bool GS_LondonDst(const MqlDateTime &utc)
{
   if(utc.mon<3 || utc.mon>10) return false;
   if(utc.mon>3 && utc.mon<10) return true;
   if(utc.mon==3)
   {
      int startDay=GS_LastSunday(utc.year,3,31);
      return utc.day>startDay || (utc.day==startDay && utc.hour>=1);
   }
   int endDay=GS_LastSunday(utc.year,10,31);
   return utc.day<endDay || (utc.day==endDay && utc.hour<1);
}

bool GS_NewYorkDst(const MqlDateTime &utc)
{
   if(utc.mon<3 || utc.mon>11) return false;
   if(utc.mon>3 && utc.mon<11) return true;
   if(utc.mon==3)
   {
      int startDay=GS_NthSunday(utc.year,3,2);
      return utc.day>startDay || (utc.day==startDay && utc.hour>=7);
   }
   int endDay=GS_NthSunday(utc.year,11,1);
   return utc.day<endDay || (utc.day==endDay && utc.hour<6);
}

bool GS_ContextMinuteInWindow(const int minuteOfDay,
                              const int openMinute,const int closeMinute)
{
   if(openMinute<0 || closeMinute<=openMinute || closeMinute>1440) return false;
   return minuteOfDay>=openMinute && minuteOfDay<closeMinute;
}

bool GS_AddMarketCenter(GoldScoutSessionContext &context,
                        const string name,const int minuteOfDay,
                        const int openMinute,const int closeMinute,
                        const datetime dayStart)
{
   if(!GS_ContextMinuteInWindow(minuteOfDay,openMinute,closeMinute)) return false;
   if(context.activeCenters!="") context.activeCenters+=", ";
   context.activeCenters+=name;
   bool opening=minuteOfDay<openMinute+90;
   if(opening) context.openingWindow=true;
   datetime openTime=dayStart+(datetime)(openMinute*60);
   if(openTime>context.referenceOpenTime) context.referenceOpenTime=openTime;
   return true;
}

bool GS_BuildSessionContext(const datetime utcNow,
                            GoldScoutSessionContext &context)
{
   GS_ClearSessionContext(context);
   if(utcNow<=0) return false;
   MqlDateTime utc;
   ZeroMemory(utc);
   if(!TimeToStruct(utcNow,utc)) return false;
   int minuteOfDay=utc.hour*60+utc.min;
   datetime dayStart=utcNow-(datetime)(utc.hour*3600+utc.min*60+utc.sec);

   // Session clock windows are meaningful only while the FX/metals week is open.
   if(utc.day_of_week==0 || utc.day_of_week==6)
   {
      context.available=true;
      context.name="MERCADO CERRADO";
      context.phase="FIN DE SEMANA";
      context.volatility="BAJA";
      int colombiaMinute=(minuteOfDay-5*60+1440)%1440;
      context.activeCenters=StringFormat(
         "UTC %02d:%02d | Colombia %02d:%02d | activos: ninguno",
         utc.hour,utc.min,colombiaMinute/60,colombiaMinute%60);
      return true;
   }

   bool tokyo=GS_AddMarketCenter(context,"TOKIO",minuteOfDay,0,9*60,dayStart);
   bool seoul=GS_AddMarketCenter(context,"SEUL",minuteOfDay,0,9*60,dayStart);
   bool shanghai=GS_AddMarketCenter(context,"SHANGHAI",minuteOfDay,60,9*60,dayStart);
   bool hongKong=GS_AddMarketCenter(context,"HONG KONG",minuteOfDay,60,9*60,dayStart);
   bool singapore=GS_AddMarketCenter(context,"SINGAPUR",minuteOfDay,60,9*60,dayStart);
   bool mumbai=GS_AddMarketCenter(context,"MUMBAI",minuteOfDay,210,690,dayStart);
   bool dubai=GS_AddMarketCenter(context,"DUBAI",minuteOfDay,300,780,dayStart);
   context.asiaActiveCenters=(tokyo?1:0)+(seoul?1:0)+(shanghai?1:0)+
      (hongKong?1:0)+(singapore?1:0)+(mumbai?1:0)+(dubai?1:0);

   bool londonDst=GS_LondonDst(utc);
   int londonOpen=londonDst?7*60:8*60;
   int londonClose=londonDst?16*60:17*60;
   context.londonActive=GS_AddMarketCenter(context,"LONDRES",minuteOfDay,
      londonOpen,londonClose,dayStart);

   bool newYorkDst=GS_NewYorkDst(utc);
   int newYorkOpen=newYorkDst?12*60+20:13*60+20;
   int newYorkClose=newYorkDst?20*60:21*60;
   context.newYorkActive=GS_AddMarketCenter(context,"NUEVA YORK/COMEX",
      minuteOfDay,newYorkOpen,newYorkClose,dayStart);

   int lbmaAmOpen=londonDst?9*60+30:10*60+30;
   int lbmaPmOpen=londonDst?14*60:15*60;
   GS_AddMarketCenter(context,"LBMA AM",minuteOfDay,lbmaAmOpen,
      lbmaAmOpen+20,dayStart);
   GS_AddMarketCenter(context,"LBMA PM",minuteOfDay,lbmaPmOpen,
      lbmaPmOpen+20,dayStart);

   context.available=true;
   if(context.londonActive && context.newYorkActive)
   {
      context.name="LONDRES+NUEVA YORK";
      context.phase="SOLAPE";
      context.volatility="ALTA";
      context.points=2;
   }
   else if(context.londonActive)
   {
      context.name="LONDRES";
      context.phase=context.openingWindow?"APERTURA":"ACTIVA";
      context.volatility="MEDIA";
      context.points=1;
   }
   else if(context.newYorkActive)
   {
      context.name="NUEVA YORK";
      context.phase=context.openingWindow?"APERTURA":"ACTIVA";
      context.volatility="MEDIA";
      context.points=1;
   }
   else if(context.asiaActiveCenters>0)
   {
      context.name="ASIA";
      context.phase=context.openingWindow?"APERTURA":"ACTIVA";
      context.volatility="BAJA";
      context.points=0;
   }
   else
   {
      context.name="TRANSICION";
      context.phase="BAJA LIQUIDEZ";
      context.volatility="BAJA";
      context.points=0;
   }

   int colombiaMinute=(minuteOfDay-5*60+1440)%1440;
   string clocks=StringFormat("UTC %02d:%02d | Colombia %02d:%02d",
      utc.hour,utc.min,colombiaMinute/60,colombiaMinute%60);
   context.activeCenters=clocks+" | activos: "+
      (context.activeCenters==""?"ninguno":context.activeCenters);
   return true;
}

// bias, declared direction and confidence form one evidence item, not three.
// Provider/data quality can cap it, while missing/stale data is only a soft -1.
int GS_NewsContextPoints(const bool enabled,const bool available,const bool fresh,
                         const int bias,const int confidence,
                         const string dataRisk,const string declaredDirection,
                         const int sourcesOk,const int sourceCount,
                         const int direction,const int configuredMaxPoints)
{
   int maxPoints=(int)MathMax(0,MathMin(
      GOLDSCOUT_MAX_NEWS_CONTEXT_POINTS,configuredMaxPoints));
   if(!enabled || maxPoints<=0 || (direction!=1 && direction!=-1)) return 0;
   if(!available || !fresh) return -1;

   string normalizedRisk=dataRisk;
   string normalizedDirection=declaredDirection;
   StringToUpper(normalizedRisk);
   StringToUpper(normalizedDirection);
   if(normalizedRisk=="HIGH") return -1;

   int boundedBias=(int)MathMax(-100,MathMin(100,bias));
   int boundedConfidence=(int)MathMax(0,MathMin(100,confidence));
   int biasDirection=boundedBias>20?1:(boundedBias<-20?-1:0);
   if(biasDirection==0 || normalizedDirection=="NEUTRO" ||
      normalizedDirection=="NEUTRAL")
      return 0;
   bool declaredAligned=(biasDirection>0 &&
      (normalizedDirection=="ALCISTA" || normalizedDirection=="LONG")) ||
      (biasDirection<0 &&
      (normalizedDirection=="BAJISTA" || normalizedDirection=="SHORT"));
   if(!declaredAligned || boundedConfidence<50) return 0;

   int points=MathMin(1,maxPoints);
   bool reliableProviders=sourceCount>=2 && sourcesOk>=2 && sourcesOk<=sourceCount;
   if(normalizedRisk=="LOW" && reliableProviders &&
      boundedConfidence>=70 && MathAbs(boundedBias)>=50)
      points=maxPoints;
   return biasDirection==direction?points:-points;
}

int GS_CombinedContextPoints(const int sessionPoints,const int newsPoints)
{
   return (int)MathMax(-GOLDSCOUT_MAX_CONTEXT_POINTS,
      MathMin(GOLDSCOUT_MAX_CONTEXT_POINTS,sessionPoints+newsPoints));
}

// Positive context cannot create an H1 setup; negative context remains a soft
// confidence adjustment and never becomes a boolean execution filter.
int GS_GatedContextPoints(const int h1TechnicalScore,const int armThreshold,
                          const int requestedPoints)
{
   int bounded=(int)MathMax(-GOLDSCOUT_MAX_CONTEXT_POINTS,
      MathMin(GOLDSCOUT_MAX_CONTEXT_POINTS,requestedPoints));
   if(bounded>0 && h1TechnicalScore<armThreshold) return 0;
   return bounded;
}

#endif
