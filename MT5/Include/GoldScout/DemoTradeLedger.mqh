#ifndef GOLDSCOUT_DEMO_TRADE_LEDGER_MQH
#define GOLDSCOUT_DEMO_TRADE_LEDGER_MQH

// DEMO-only append ledger. This is an evidence/index layer, not an order path.
uint GSDL_Hash(const string value)
{
   uint hash=2166136261;
   for(int i=0;i<StringLen(value);i++)
   {
      hash^=(uint)StringGetCharacter(value,i);
      hash*=16777619;
   }
   return hash;
}

string GSDL_TradeId(const long account,const string symbol,const long magic,
                    const string signalId,const ulong positionId)
{
   if(signalId!="")
      return StringFormat("GST-%I64d-%I64d-%s-%s",account,magic,symbol,signalId);
   return StringFormat("GST-ORPHAN-%I64d-%I64d-%s-%I64u",
      account,magic,symbol,positionId);
}

string GSDL_CanonicalEvent(const string eventType)
{
   if(eventType=="ORDER_REQUEST") return "ORDER_REQUESTED";
   if(eventType=="POSITION_PARTIAL_CLOSE") return "PARTIALLY_CLOSED";
   if(eventType=="POSITION_CLOSE") return "POSITION_CLOSED";
   return eventType;
}

string GSDL_EventId(const string eventType,const string tradeId,const ulong orderId,
                    const ulong dealId,const ulong positionId,const string detail="")
{
   string kind=GSDL_CanonicalEvent(eventType);
   string key=tradeId+"|"+kind;
   if(kind=="ORDER_FILLED" || kind=="PARTIALLY_CLOSED")
      key=StringFormat("%I64d|%s|P%I64u|D%I64u",
         AccountInfoInteger(ACCOUNT_LOGIN),kind,positionId,dealId);
   else if(kind=="POSITION_OPEN" || kind=="POSITION_CLOSED" ||
           kind=="RECONCILED" || kind=="RECONCILIATION_ERROR")
      key=StringFormat("%I64d|%s|%s|P%I64u|%s",
         AccountInfoInteger(ACCOUNT_LOGIN),kind,tradeId,positionId,detail);
   else if(kind=="ORDER_REJECTED")
      key+=StringFormat("|O%I64u|R%u",orderId,GSDL_Hash(detail));
   return "GSE2-"+key;
}

double GSDL_SignedSlippage(const string direction,const double requested,
                           const double executed)
{
   if(requested<=0.0 || executed<=0.0) return 0.0;
   if(direction=="LONG") return executed-requested;
   if(direction=="SHORT") return requested-executed;
   return 0.0;
}

string GSDL_JsonField(const string json,const string key)
{
   string marker="\""+key+"\":";
   int start=StringFind(json,marker);
   if(start<0) return "";
   start+=StringLen(marker);
   if(start>=StringLen(json)) return "";
   if(StringGetCharacter(json,start)=='"')
   {
      start++;
      int finish=StringFind(json,"\"",start);
      return finish>=start?StringSubstr(json,start,finish-start):"";
   }
   int comma=StringFind(json,",",start);
   int brace=StringFind(json,"}",start);
   int finish=comma>=0?comma:brace;
   if(brace>=0 && (finish<0 || brace<finish)) finish=brace;
   return finish>=start?StringSubstr(json,start,finish-start):"";
}

bool GSDL_JsonNumber(const string json,const string key,double &value)
{
   value=0.0;
   string raw=GSDL_JsonField(json,key);
   if(raw=="" || raw=="null") return false;
   ushort first=StringGetCharacter(raw,0);
   if((first<'0' || first>'9') && first!='-') return false;
   value=StringToDouble(raw);
   return MathIsValidNumber(value);
}

bool GSDL_ValidJsonObject(const string json)
{
   int length=StringLen(json);
   if(length<2 || StringGetCharacter(json,0)!='{' ||
      StringGetCharacter(json,length-1)!='}') return false;
   int depth=0;
   bool quoted=false,escaped=false;
   for(int i=0;i<length;i++)
   {
      ushort c=StringGetCharacter(json,i);
      if(quoted)
      {
         if(escaped) { escaped=false; continue; }
         if(c=='\\') { escaped=true; continue; }
         if(c=='"') quoted=false;
         continue;
      }
      if(c=='"') { quoted=true; continue; }
      if(c=='{') depth++;
      if(c=='}') depth--;
      if(depth<0) return false;
   }
   return !quoted && !escaped && depth==0;
}

struct GSDL_PositionState
{
   ulong positionId;
   string tradeId;
   string signalId;
   string status;
   bool closedSeen;
   string setup;
   string tradeClass;
   string direction;
   int score;
   double initialEntry;
   double entryValue;
   double entryVolume;
   double initialSL;
   double initialTP;
   double initialRisk;
   bool riskKnown;
   double maxFavorable;
   double maxAdverse;
   bool excursionObserved;
   double slippageWeighted;
   double slippageVolume;
   int fillCount;
   string excursionQuality;
};

class GoldScoutDemoLedger
{
private:
   string m_filename;
   string m_ids[];
   int m_buckets[];
   string m_ambiguousTrades[];
   GSDL_PositionState m_positions[];
   bool m_healthy;
   int m_sequence;
   long m_account;

   void RebuildBuckets(const int capacity)
   {
      ArrayResize(m_buckets,capacity);
      ArrayInitialize(m_buckets,-1);
      for(int i=0;i<ArraySize(m_ids);i++)
      {
         int slot=(int)(GSDL_Hash(m_ids[i])%(uint)capacity);
         while(m_buckets[slot]>=0) slot=(slot+1)%capacity;
         m_buckets[slot]=i;
      }
   }

   int PositionIndex(const ulong positionId)
   {
      for(int i=0;i<ArraySize(m_positions);i++)
         if(m_positions[i].positionId==positionId) return i;
      return -1;
   }

   void Remember(const string json,const string eventId)
   {
      int size=ArraySize(m_ids);
      ArrayResize(m_ids,size+1);
      m_ids[size]=eventId;
      if(ArraySize(m_buckets)<1024 || (size+1)*2>=ArraySize(m_buckets))
      {
         int capacity=ArraySize(m_buckets)*2;
         if(capacity<1024) capacity=1024;
         RebuildBuckets(capacity);
      }
      else
      {
         int slot=(int)(GSDL_Hash(eventId)%(uint)ArraySize(m_buckets));
         while(m_buckets[slot]>=0) slot=(slot+1)%ArraySize(m_buckets);
         m_buckets[slot]=size;
      }
      m_sequence++;
      double recordedSequence=0.0;
      if(GSDL_JsonNumber(json,"sequence",recordedSequence) &&
         recordedSequence>m_sequence && recordedSequence<2147483647.0)
         m_sequence=(int)recordedSequence;
      string recordAccount=GSDL_JsonField(json,"account_login");
      if(recordAccount!="" && (long)StringToInteger(recordAccount)!=m_account) return;
      string tradeIdForOrder=GSDL_JsonField(json,"trade_id");
      string eventForOrder=GSDL_CanonicalEvent(GSDL_JsonField(json,"event"));
      if(eventForOrder=="RECONCILIATION_ERROR" &&
         GSDL_JsonField(json,"reconciliation_status")=="AMBIGUOUS_ORDER_RESULT" &&
         tradeIdForOrder!="")
      {
         bool found=false;
         for(int i=0;i<ArraySize(m_ambiguousTrades);i++)
            if(m_ambiguousTrades[i]==tradeIdForOrder) { found=true; break; }
         if(!found)
         {
            int count=ArraySize(m_ambiguousTrades);
            ArrayResize(m_ambiguousTrades,count+1);
            m_ambiguousTrades[count]=tradeIdForOrder;
         }
      }
      if(eventForOrder=="ORDER_FILLED" && tradeIdForOrder!="")
      {
         for(int i=0;i<ArraySize(m_ambiguousTrades);i++)
            if(m_ambiguousTrades[i]==tradeIdForOrder)
            {
               int last=ArraySize(m_ambiguousTrades)-1;
               m_ambiguousTrades[i]=m_ambiguousTrades[last];
               ArrayResize(m_ambiguousTrades,last);
               break;
            }
      }
      ulong positionId=(ulong)StringToInteger(GSDL_JsonField(json,"position_identifier"));
      if(positionId==0)
         positionId=(ulong)StringToInteger(GSDL_JsonField(json,"position_id"));
      if(positionId==0) return;
      int index=PositionIndex(positionId);
      if(index<0)
      {
         index=ArraySize(m_positions);
         ArrayResize(m_positions,index+1);
         m_positions[index].positionId=positionId;
         m_positions[index].status="UNKNOWN";
         m_positions[index].closedSeen=false;
         m_positions[index].setup="UNKNOWN";
         m_positions[index].tradeClass="UNKNOWN";
         m_positions[index].direction="UNKNOWN";
         m_positions[index].score=0;
         m_positions[index].initialEntry=0.0;
         m_positions[index].entryValue=0.0;
         m_positions[index].entryVolume=0.0;
         m_positions[index].initialSL=0.0;
         m_positions[index].initialTP=0.0;
         m_positions[index].initialRisk=0.0;
         m_positions[index].riskKnown=false;
         m_positions[index].maxFavorable=0.0;
         m_positions[index].maxAdverse=0.0;
         m_positions[index].excursionObserved=false;
         m_positions[index].slippageWeighted=0.0;
         m_positions[index].slippageVolume=0.0;
         m_positions[index].fillCount=0;
         m_positions[index].excursionQuality="UNKNOWN";
      }
      string tradeId=GSDL_JsonField(json,"trade_id");
      string signalId=GSDL_JsonField(json,"signal_event_id");
      if(tradeId!="") m_positions[index].tradeId=tradeId;
      if(signalId!="") m_positions[index].signalId=signalId;
      string setup=GSDL_JsonField(json,"setup");
      string tradeClass=GSDL_JsonField(json,"class");
      string direction=GSDL_JsonField(json,"direction");
      if(setup!="" && setup!="UNKNOWN") m_positions[index].setup=setup;
      if(tradeClass!="" && tradeClass!="UNKNOWN") m_positions[index].tradeClass=tradeClass;
      if(direction!="" && direction!="UNKNOWN") m_positions[index].direction=direction;
      string score=GSDL_JsonField(json,"score");
      if(score!="") m_positions[index].score=(int)StringToInteger(score);
      string kind=GSDL_CanonicalEvent(GSDL_JsonField(json,"event"));
      if(kind=="POSITION_OPEN") m_positions[index].status="OPEN";
      if(kind=="POSITION_CLOSED")
      {
         m_positions[index].status="CLOSED";
         m_positions[index].closedSeen=true;
      }
      if(kind=="RECONCILIATION_ERROR") m_positions[index].status="ERROR";
      if(kind=="RECONCILED" && !m_positions[index].closedSeen &&
         (GSDL_JsonField(json,"reconciliation_status")=="RECOVERED_OK" ||
          GSDL_JsonField(json,"reconciliation_status")=="RECOVERED_ORPHAN" ||
          GSDL_JsonField(json,"reconciliation_status")=="RECOVERED_AFTER_ERROR"))
         m_positions[index].status="OPEN";
      if(kind=="ORDER_FILLED")
      {
         m_positions[index].fillCount++;
         double value=0.0;
         if(GSDL_JsonNumber(json,"initial_risk_account_currency",value) && value>0.0)
         {
            m_positions[index].initialRisk+=value;
            m_positions[index].riskKnown=true;
         }
         double fillPrice=0.0,fillVolume=0.0;
         if(GSDL_JsonNumber(json,"executed_price",fillPrice) && fillPrice>0.0 &&
            GSDL_JsonNumber(json,"filled_volume",fillVolume) && fillVolume>0.0)
         {
            m_positions[index].entryValue+=fillPrice*fillVolume;
            m_positions[index].entryVolume+=fillVolume;
            m_positions[index].initialEntry=m_positions[index].entryValue/
               m_positions[index].entryVolume;
         }
         if(m_positions[index].initialSL<=0.0 && GSDL_JsonNumber(json,"initial_sl",value))
            m_positions[index].initialSL=value;
         if(m_positions[index].initialTP<=0.0 && GSDL_JsonNumber(json,"initial_tp",value))
            m_positions[index].initialTP=value;
         double slip=0.0,volume=0.0;
         if(GSDL_JsonNumber(json,"entry_slippage_price",slip) &&
            GSDL_JsonNumber(json,"filled_volume",volume) && volume>0.0)
         {
            m_positions[index].slippageWeighted+=slip*volume;
            m_positions[index].slippageVolume+=volume;
         }
      }
      if(kind=="RECONCILED" && GSDL_JsonField(json,"reconciliation_status")=="EXCURSION_SAMPLE")
      {
         double value=0.0;
         if(GSDL_JsonField(json,"excursion_observed")=="true")
            m_positions[index].excursionObserved=true;
         if(m_positions[index].excursionObserved &&
            GSDL_JsonNumber(json,"max_favorable_excursion_price",value))
            m_positions[index].maxFavorable=MathMax(m_positions[index].maxFavorable,value);
         if(m_positions[index].excursionObserved &&
            GSDL_JsonNumber(json,"max_adverse_excursion_price",value))
            m_positions[index].maxAdverse=MathMax(m_positions[index].maxAdverse,value);
         string quality=GSDL_JsonField(json,"mfe_mae_quality");
         m_positions[index].excursionQuality=quality!=""?quality:"PARTIAL";
      }
   }

public:
   GoldScoutDemoLedger(void) { m_healthy=true; m_sequence=0; m_account=0; }
   bool Healthy(void) { return m_healthy; }
   int NextSequence(void) { return m_sequence+1; }
   int PositionCount(void) { return ArraySize(m_positions); }
   bool AmbiguousOrders(void) { return ArraySize(m_ambiguousTrades)>0; }
   GSDL_PositionState PositionAt(const int index) { return m_positions[index]; }
   bool StateFor(const ulong positionId,GSDL_PositionState &state)
   {
      int index=PositionIndex(positionId);
      if(index<0) return false;
      state=m_positions[index];
      return true;
   }
   bool HasEvent(const string eventId)
   {
      int capacity=ArraySize(m_buckets);
      if(capacity<=0) return false;
      int slot=(int)(GSDL_Hash(eventId)%(uint)capacity);
      for(int i=0;i<capacity;i++)
      {
         int index=m_buckets[slot];
         if(index<0) return false;
         if(m_ids[index]==eventId) return true;
         slot=(slot+1)%capacity;
      }
      return false;
   }
   bool Initialize(const string filename)
   {
      m_filename=filename;
      m_account=AccountInfoInteger(ACCOUNT_LOGIN);
      m_healthy=true;
      m_sequence=0;
      ArrayResize(m_ids,0);
      ArrayResize(m_ambiguousTrades,0);
      RebuildBuckets(1024);
      ArrayResize(m_positions,0);
      if(!FileIsExist(filename,FILE_COMMON)) return true;
      int handle=FileOpen(filename,FILE_COMMON|FILE_READ|FILE_TXT|FILE_ANSI|
         FILE_SHARE_READ|FILE_SHARE_WRITE,0,CP_UTF8);
      if(handle==INVALID_HANDLE) { m_healthy=false; return false; }
      while(!FileIsEnding(handle))
      {
         string line=FileReadString(handle);
         StringTrimLeft(line);
         StringTrimRight(line);
         if(StringLen(line)>0 && StringGetCharacter(line,0)==65279)
            line=StringSubstr(line,1);
         if(line=="") continue;
         string eventId=GSDL_JsonField(line,"event_id");
         string eventType=GSDL_JsonField(line,"event");
         if(!GSDL_ValidJsonObject(line) || eventId=="" ||
            eventType=="" || GSDL_JsonField(line,"source")=="")
         {
            m_healthy=false;
            break;
         }
         if(!HasEvent(eventId)) Remember(line,eventId);
      }
      FileClose(handle);
      return m_healthy;
   }
   bool Append(const string eventId,const string json)
   {
      if(!m_healthy) return false;
      if(HasEvent(eventId)) return true;
      int handle=FileOpen(m_filename,FILE_COMMON|FILE_READ|FILE_WRITE|FILE_TXT|
         FILE_ANSI|FILE_SHARE_READ,0,CP_UTF8);
      if(handle==INVALID_HANDLE) { m_healthy=false; return false; }
      // The writer lock serializes terminals sharing Common/Files. Refresh a
      // bounded recent tail because another EA instance may have appended
      // after this instance loaded its in-memory index.
      long size=(long)FileSize(handle);
      long start=MathMax((long)0,size-(long)(256*1024));
      if(!FileSeek(handle,start,SEEK_SET))
      { FileClose(handle); m_healthy=false; return false; }
      if(start>0 && !FileIsEnding(handle)) FileReadString(handle);
      while(!FileIsEnding(handle))
      {
         string line=FileReadString(handle);
         StringTrimRight(line);
         if(StringLen(line)>0 && StringGetCharacter(line,0)==65279)
            line=StringSubstr(line,1);
         if(line=="") continue;
         if(!GSDL_ValidJsonObject(line))
         { FileClose(handle); m_healthy=false; return false; }
         string existingId=GSDL_JsonField(line,"event_id");
         if(existingId==eventId)
         {
            if(!HasEvent(eventId)) Remember(line,eventId);
            FileClose(handle);
            return true;
         }
      }
      if(!FileSeek(handle,0,SEEK_END)) { FileClose(handle); m_healthy=false; return false; }
      string record=json+"\r\n";
      uint written=FileWriteString(handle,record);
      FileFlush(handle);
      FileClose(handle);
      if(written<(uint)StringLen(record)) { m_healthy=false; return false; }
      Remember(json,eventId);
      return true;
   }
   void MarkUnsafe(void) { m_healthy=false; }
};

#endif
