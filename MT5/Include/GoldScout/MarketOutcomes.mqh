#ifndef GOLDSCOUT_MARKET_OUTCOMES_MQH
#define GOLDSCOUT_MARKET_OUTCOMES_MQH

// Forward labels are expressed as decimal returns from a LONG-XAUUSD view:
// +0.01 means gold rose 1%, MFE is >= 0 and MAE is <= 0. Consumers evaluating
// a SHORT hypothesis invert the signed return. Only fully closed M1 bars after
// the observation anchor are used.
const int GOLDSCOUT_OUTCOME_HORIZON_SECONDS[3]={15*60,60*60,4*60*60};
const string GOLDSCOUT_OUTCOME_HORIZON_NAMES[3]={"15m","1h","4h"};
const int GOLDSCOUT_OUTCOME_ALL_COMPLETE=7;
const long GOLDSCOUT_OUTCOME_RECOVERY_BYTES=32*1024*1024;

enum ENUM_GOLDSCOUT_OUTCOME_STATE
{
   GOLDSCOUT_OUTCOME_PENDING=0,
   GOLDSCOUT_OUTCOME_RESOLVABLE=1,
   GOLDSCOUT_OUTCOME_UNRESOLVABLE_GAP=2,
   GOLDSCOUT_OUTCOME_COMPLETED=3
};

struct GoldScoutPendingOutcome
{
   string   eventId;
   datetime anchorAt;
   double   initialPrice;
   int      completedMask;
   double   futureReturn[3];
   double   mfe[3];
   double   mae[3];
   bool     haveValue[3];
};

string GSMOL_JsonEscape(string value)
{
   StringReplace(value,"\\","\\\\");
   StringReplace(value,"\"","\\\"");
   StringReplace(value,"\r"," ");
   StringReplace(value,"\n"," ");
   StringReplace(value,"\t"," ");
   return value;
}

bool GSMOL_ExtractString(const string json,const string key,string &value)
{
   string marker="\""+key+"\":\"";
   int begin=StringFind(json,marker);
   if(begin<0) return false;
   begin+=StringLen(marker);
   int finish=StringFind(json,"\"",begin);
   if(finish<=begin) return false;
   value=StringSubstr(json,begin,finish-begin);
   return value!="";
}

bool GSMOL_ExtractNumberToken(const string json,const string key,string &token)
{
   string marker="\""+key+"\":";
   int begin=StringFind(json,marker);
   if(begin<0) return false;
   begin+=StringLen(marker);
   int comma=StringFind(json,",",begin);
   int brace=StringFind(json,"}",begin);
   int finish=comma;
   if(finish<0 || (brace>=0 && brace<finish)) finish=brace;
   if(finish<=begin) return false;
   token=StringSubstr(json,begin,finish-begin);
   StringTrimLeft(token);
   StringTrimRight(token);
   return token!="" && token!="null";
}

bool GSMOL_ExtractLong(const string json,const string key,long &value)
{
   string token="";
   if(!GSMOL_ExtractNumberToken(json,key,token)) return false;
   value=StringToInteger(token);
   return value>0;
}

bool GSMOL_ExtractDouble(const string json,const string key,double &value)
{
   string token="";
   if(!GSMOL_ExtractNumberToken(json,key,token)) return false;
   value=StringToDouble(token);
   return MathIsValidNumber(value);
}

class GoldScoutMarketOutcomeLabeler
{
private:
   string m_symbol;
   string m_observationFilename;
   string m_outcomeFilename;
   string m_recentOutcomeIds[];
   int    m_recentLimit;
   int    m_recentCursor;
   GoldScoutPendingOutcome m_pending[];
   bool   m_initialized;
   datetime m_lastErrorLog;

   string OutcomeId(const string eventId,const int horizonIndex) const
   {
      return eventId+"|"+GOLDSCOUT_OUTCOME_HORIZON_NAMES[horizonIndex];
   }

   bool IsRecentOutcomeId(const string outcomeId) const
   {
      for(int i=0;i<ArraySize(m_recentOutcomeIds);i++)
         if(m_recentOutcomeIds[i]==outcomeId) return true;
      return false;
   }

   void RememberOutcomeId(const string outcomeId)
   {
      if(outcomeId=="" || IsRecentOutcomeId(outcomeId)) return;
      int size=ArraySize(m_recentOutcomeIds);
      if(size<m_recentLimit)
      {
         if(ArrayResize(m_recentOutcomeIds,size+1)==size+1)
            m_recentOutcomeIds[size]=outcomeId;
         return;
      }
      if(size<=0) return;
      m_recentOutcomeIds[m_recentCursor]=outcomeId;
      m_recentCursor=(m_recentCursor+1)%size;
   }

   void ReadRecentOutcomeIds()
   {
      int handle=FileOpen(m_outcomeFilename,FILE_COMMON|FILE_READ|FILE_TXT|FILE_ANSI|FILE_SHARE_READ|FILE_SHARE_WRITE,0,CP_UTF8);
      if(handle==INVALID_HANDLE) return;
      long size=(long)FileSize(handle);
      long start=MathMax((long)0,size-GOLDSCOUT_OUTCOME_RECOVERY_BYTES);
      if(!FileSeek(handle,start,SEEK_SET))
      {
         FileClose(handle);
         return;
      }
      if(start>0 && !FileIsEnding(handle)) FileReadString(handle);
      while(!FileIsEnding(handle))
      {
         string line=FileReadString(handle),outcomeId="";
         if(GSMOL_ExtractString(line,"outcome_id",outcomeId))
            RememberOutcomeId(outcomeId);
      }
      FileClose(handle);
   }

   int CompletedMask(const string eventId) const
   {
      int mask=0;
      for(int i=0;i<3;i++)
         if(IsRecentOutcomeId(OutcomeId(eventId,i))) mask|=(1<<i);
      return mask;
   }

   int PendingIndex(const string eventId) const
   {
      for(int i=0;i<ArraySize(m_pending);i++)
         if(m_pending[i].eventId==eventId) return i;
      return -1;
   }

   void ClearPendingValue(GoldScoutPendingOutcome &pending)
   {
      pending.eventId="";
      pending.anchorAt=0;
      pending.initialPrice=0.0;
      pending.completedMask=0;
      for(int i=0;i<3;i++)
      {
         pending.futureReturn[i]=0.0;
         pending.mfe[i]=0.0;
         pending.mae[i]=0.0;
         pending.haveValue[i]=false;
      }
   }

   void RemovePending(const int index)
   {
      int size=ArraySize(m_pending);
      if(index<0 || index>=size) return;
      for(int i=index;i<size-1;i++) m_pending[i]=m_pending[i+1];
      ArrayResize(m_pending,size-1);
   }

   datetime DeriveAnchor(const string line)
   {
      long explicitAnchor=0;
      if(GSMOL_ExtractLong(line,"outcome_anchor_at",explicitAnchor))
         return (datetime)explicitAnchor;

      long capturedAt=0,timestamp=0;
      string snapshotType="",timeframe="";
      if(!GSMOL_ExtractLong(line,"captured_at",capturedAt)) return 0;
      if(!GSMOL_ExtractString(line,"snapshot_type",snapshotType) || snapshotType!="BAR_CLOSE")
         return (datetime)capturedAt;
      if(!GSMOL_ExtractLong(line,"timestamp",timestamp) ||
         !GSMOL_ExtractString(line,"timeframe",timeframe)) return 0;
      int seconds=0;
      if(timeframe=="M15") seconds=15*60;
      else if(timeframe=="H1") seconds=60*60;
      else if(timeframe=="H4") seconds=4*60*60;
      if(seconds<=0) return 0;
      return (datetime)(timestamp+seconds);
   }

   void RecoverPendingObservations()
   {
      int handle=FileOpen(m_observationFilename,FILE_COMMON|FILE_READ|FILE_TXT|FILE_ANSI|FILE_SHARE_READ|FILE_SHARE_WRITE,0,CP_UTF8);
      if(handle==INVALID_HANDLE) return;
      long size=(long)FileSize(handle);
      long start=MathMax((long)0,size-GOLDSCOUT_OUTCOME_RECOVERY_BYTES);
      if(!FileSeek(handle,start,SEEK_SET))
      {
         FileClose(handle);
         return;
      }
      if(start>0 && !FileIsEnding(handle)) FileReadString(handle);
      while(!FileIsEnding(handle))
      {
         string line=FileReadString(handle),eventId="";
         double initialPrice=0.0;
         if(!GSMOL_ExtractString(line,"event_id",eventId) ||
            !GSMOL_ExtractDouble(line,"close",initialPrice) || initialPrice<=0.0)
            continue;
         datetime anchorAt=DeriveAnchor(line);
         if(anchorAt<=0) continue;
         Track(eventId,anchorAt,initialPrice);
      }
      FileClose(handle);
   }

   void LogPersistenceError()
   {
      datetime now=TimeTradeServer();
      if(now<=0) now=TimeCurrent();
      if(now<=0 || (m_lastErrorLog>0 && (long)(now-m_lastErrorLog)<60)) return;
      m_lastErrorLog=now;
      PrintFormat("[GoldScout][MARKET_OUTCOMES] persistence error=%d | trading remains unaffected",GetLastError());
   }

   bool HistoryAdvancedBeyond(const datetime endMinute) const
   {
      long synchronized=0,lastBarDate=0;
      if(!SeriesInfoInteger(m_symbol,PERIOD_M1,SERIES_SYNCHRONIZED,synchronized) ||
         synchronized==0) return false;
      if(!SeriesInfoInteger(m_symbol,PERIOD_M1,SERIES_LASTBAR_DATE,lastBarDate))
         return false;
      return lastBarDate>(long)endMinute;
   }

   ENUM_GOLDSCOUT_OUTCOME_STATE EvaluateHorizon(
                         const GoldScoutPendingOutcome &pending,const int horizonIndex,
                         const datetime now,double &futureReturn,double &mfe,double &mae)
   {
      if(horizonIndex<0 || horizonIndex>=3 || pending.anchorAt<=0 || pending.initialPrice<=0.0)
         return GOLDSCOUT_OUTCOME_PENDING;
      datetime target=(datetime)((long)pending.anchorAt+GOLDSCOUT_OUTCOME_HORIZON_SECONDS[horizonIndex]);
      if(now<target) return GOLDSCOUT_OUTCOME_PENDING;
      datetime startMinute=(datetime)((((long)pending.anchorAt+59)/60)*60);
      datetime endMinute=(datetime)(((long)target/60)*60-60);
      if(endMinute<startMinute) return GOLDSCOUT_OUTCOME_PENDING;

      MqlRates bars[];
      ArraySetAsSeries(bars,false);
      int copied=CopyRates(m_symbol,PERIOD_M1,startMinute,endMinute,bars);
      int expected=(int)(((long)endMinute-(long)startMinute)/60+1);
      bool complete=(copied==expected && copied>0);
      double maximum=pending.initialPrice,minimum=pending.initialPrice;
      if(complete)
      {
         for(int i=0;i<copied;i++)
         {
            datetime expectedTime=(datetime)((long)startMinute+i*60);
            if(bars[i].time!=expectedTime || bars[i].high<bars[i].low ||
               bars[i].high<=0.0 || bars[i].low<=0.0 || bars[i].close<=0.0)
            {
               complete=false;
               break;
            }
            maximum=MathMax(maximum,bars[i].high);
            minimum=MathMin(minimum,bars[i].low);
         }
      }
      if(complete)
      {
         futureReturn=(bars[copied-1].close-pending.initialPrice)/pending.initialPrice;
         mfe=MathMax(0.0,(maximum-pending.initialPrice)/pending.initialPrice);
         mae=MathMin(0.0,(minimum-pending.initialPrice)/pending.initialPrice);
         if(MathIsValidNumber(futureReturn) && MathIsValidNumber(mfe) && MathIsValidNumber(mae))
            return GOLDSCOUT_OUTCOME_RESOLVABLE;
      }
      if(HistoryAdvancedBeyond(endMinute))
         return GOLDSCOUT_OUTCOME_UNRESOLVABLE_GAP;
      return GOLDSCOUT_OUTCOME_PENDING;
   }

   string ValueOrNull(const GoldScoutPendingOutcome &pending,const int horizonIndex,
                      const int valueType) const
   {
      if(horizonIndex<0 || horizonIndex>=3 || !pending.haveValue[horizonIndex]) return "null";
      if(valueType==0) return DoubleToString(pending.futureReturn[horizonIndex],10);
      if(valueType==1) return DoubleToString(pending.mfe[horizonIndex],10);
      return DoubleToString(pending.mae[horizonIndex],10);
   }

   bool AppendOutcome(const GoldScoutPendingOutcome &pending,const int horizonIndex,
                      const datetime evaluatedAt,const ENUM_GOLDSCOUT_OUTCOME_STATE state)
   {
      string outcomeId=OutcomeId(pending.eventId,horizonIndex);
      if(IsRecentOutcomeId(outcomeId)) return true;
      string status=(state==GOLDSCOUT_OUTCOME_UNRESOLVABLE_GAP?
         "UNRESOLVABLE_GAP":"COMPLETED");
      string json="{";
      json+="\"outcome_id\":\""+GSMOL_JsonEscape(outcomeId)+"\",";
      json+="\"event_id\":\""+GSMOL_JsonEscape(pending.eventId)+"\",";
      json+=StringFormat("\"evaluated_at\":%I64d,",(long)evaluatedAt);
      json+="\"source\":\"MT5\",\"observer_only\":true,\"score_effect\":0,";
      json+="\"return_convention\":\"LONG_XAUUSD_DECIMAL\",";
      json+="\"status\":\""+status+"\",";
      json+="\"horizon\":\""+GOLDSCOUT_OUTCOME_HORIZON_NAMES[horizonIndex]+"\",";
      json+="\"completed_horizon\":\""+GOLDSCOUT_OUTCOME_HORIZON_NAMES[horizonIndex]+"\",";
      json+="\"future_return_15m\":"+ValueOrNull(pending,0,0)+",";
      json+="\"future_return_1h\":"+ValueOrNull(pending,1,0)+",";
      json+="\"future_return_4h\":"+ValueOrNull(pending,2,0)+",";
      json+="\"mfe_15m\":"+ValueOrNull(pending,0,1)+",\"mae_15m\":"+ValueOrNull(pending,0,2)+",";
      json+="\"mfe_1h\":"+ValueOrNull(pending,1,1)+",\"mae_1h\":"+ValueOrNull(pending,1,2)+",";
      json+="\"mfe_4h\":"+ValueOrNull(pending,2,1)+",\"mae_4h\":"+ValueOrNull(pending,2,2);
      json+="}";

      ResetLastError();
      int handle=FileOpen(m_outcomeFilename,FILE_COMMON|FILE_READ|FILE_WRITE|FILE_TXT|FILE_ANSI|FILE_SHARE_READ|FILE_SHARE_WRITE,0,CP_UTF8);
      if(handle==INVALID_HANDLE)
      {
         LogPersistenceError();
         return false;
      }
      bool ok=FileSeek(handle,0,SEEK_END);
      if(ok) ok=FileWriteString(handle,json+"\n")>0;
      if(ok) FileFlush(handle);
      FileClose(handle);
      if(!ok)
      {
         LogPersistenceError();
         return false;
      }
      RememberOutcomeId(outcomeId);
      return true;
   }

public:
   GoldScoutMarketOutcomeLabeler()
   {
      m_symbol="";
      m_observationFilename="market_observations.jsonl";
      m_outcomeFilename="market_outcomes.jsonl";
      m_recentLimit=50000;
      m_recentCursor=0;
      m_initialized=false;
      m_lastErrorLog=0;
   }

   bool Initialize(const string symbol,const string observationFilename,
                   const string outcomeFilename)
   {
      Shutdown();
      if(symbol=="" || observationFilename=="" || outcomeFilename=="" ||
         StringCompare(observationFilename,outcomeFilename,false)==0) return false;
      m_symbol=symbol;
      m_observationFilename=observationFilename;
      m_outcomeFilename=outcomeFilename;
      m_initialized=true;
      ReadRecentOutcomeIds();
      RecoverPendingObservations();
      return true;
   }

   void Shutdown()
   {
      ArrayResize(m_recentOutcomeIds,0);
      ArrayResize(m_pending,0);
      m_recentCursor=0;
      m_initialized=false;
   }

   bool IsInitialized() const { return m_initialized; }

   void Track(const string eventId,const datetime anchorAt,const double initialPrice)
   {
      if(!m_initialized || eventId=="" || anchorAt<=0 || initialPrice<=0.0 ||
         PendingIndex(eventId)>=0) return;
      int mask=CompletedMask(eventId);
      if(mask==GOLDSCOUT_OUTCOME_ALL_COMPLETE) return;
      GoldScoutPendingOutcome pending;
      ClearPendingValue(pending);
      pending.eventId=eventId;
      pending.anchorAt=anchorAt;
      pending.initialPrice=initialPrice;
      pending.completedMask=mask;
      int size=ArraySize(m_pending);
      if(ArrayResize(m_pending,size+1)==size+1) m_pending[size]=pending;
   }

   void Poll(const int maximumEvents=8)
   {
      if(!m_initialized || maximumEvents<=0) return;
      datetime now=TimeTradeServer();
      if(now<=0) now=TimeCurrent();
      if(now<=0) return;

      int usefulWork=0;
      for(int i=0;i<ArraySize(m_pending) && usefulWork<maximumEvents;)
      {
         for(int horizon=0;horizon<3 && usefulWork<maximumEvents;horizon++)
         {
            int bit=(1<<horizon);
            if((m_pending[i].completedMask&bit)!=0) continue;
            double futureReturn=0.0,mfe=0.0,mae=0.0;
            ENUM_GOLDSCOUT_OUTCOME_STATE state=EvaluateHorizon(
               m_pending[i],horizon,now,futureReturn,mfe,mae);
            if(state==GOLDSCOUT_OUTCOME_PENDING)
               break;
            if(state==GOLDSCOUT_OUTCOME_RESOLVABLE)
            {
               m_pending[i].futureReturn[horizon]=futureReturn;
               m_pending[i].mfe[horizon]=mfe;
               m_pending[i].mae[horizon]=mae;
               m_pending[i].haveValue[horizon]=true;
            }
            else
               m_pending[i].haveValue[horizon]=false;
            if(AppendOutcome(m_pending[i],horizon,now,state))
            {
               m_pending[i].completedMask|=bit;
               usefulWork++;
            }
            else
               break;
         }
         if(m_pending[i].completedMask==GOLDSCOUT_OUTCOME_ALL_COMPLETE)
            RemovePending(i);
         else
            i++;
      }
   }
};

#endif
