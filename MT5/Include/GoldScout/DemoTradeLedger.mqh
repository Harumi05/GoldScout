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

bool GSDL_ParseString(const string json,int &at,string &value,const bool key);
void GSDL_SkipSpace(const string json,int &at);

string GSDL_JsonField(const string json,const string key)
{
   string marker="\""+key+"\":";
   int start=StringFind(json,marker);
   if(start<0) return "";
   start+=StringLen(marker);
   GSDL_SkipSpace(json,start);
   if(start>=StringLen(json)) return "";
   if(StringGetCharacter(json,start)=='"')
   {
      string value="";
      return GSDL_ParseString(json,start,value,false)?value:"";
   }
   int comma=StringFind(json,",",start);
   int brace=StringFind(json,"}",start);
   int finish=comma>=0?comma:brace;
   if(brace>=0 && (finish<0 || brace<finish)) finish=brace;
   return finish>=start?StringSubstr(json,start,finish-start):"";
}

bool GSDL_IsDigit(const ushort c) { return c>='0' && c<='9'; }
bool GSDL_IsHex(const ushort c)
{
   return GSDL_IsDigit(c) || (c>='a' && c<='f') || (c>='A' && c<='F');
}

void GSDL_SkipSpace(const string json,int &at)
{
   while(at<StringLen(json))
   {
      ushort c=StringGetCharacter(json,at);
      if(c!=' ' && c!='\t' && c!='\r' && c!='\n') break;
      at++;
   }
}

bool GSDL_ParseString(const string json,int &at,string &value,const bool key)
{
   value="";
   int length=StringLen(json);
   if(at>=length || StringGetCharacter(json,at)!='"') return false;
   at++;
   while(at<length)
   {
      ushort c=StringGetCharacter(json,at++);
      if(c=='"') return true;
      if(c<32) return false;
      if(c!='\\')
      {
         if(key && !((c>='a' && c<='z') || (c>='A' && c<='Z') ||
            GSDL_IsDigit(c) || c=='_')) return false;
         value+=ShortToString(c);
         continue;
      }
      if(key || at>=length) return false; // Schema keys are literal ASCII.
      ushort escaped=StringGetCharacter(json,at++);
      if(escaped=='"' || escaped=='\\' || escaped=='/')
         value+=ShortToString(escaped);
      else if(escaped=='b') value+=ShortToString(8);
      else if(escaped=='f') value+=ShortToString(12);
      else if(escaped=='n') value+="\n";
      else if(escaped=='r') value+="\r";
      else if(escaped=='t') value+="\t";
      else if(escaped=='u')
      {
         if(at+4>length) return false;
         int code=0;
         for(int j=0;j<4;j++)
         {
            ushort digit=StringGetCharacter(json,at++);
            if(!GSDL_IsHex(digit)) return false;
            code=code*16+(GSDL_IsDigit(digit)?(int)(digit-'0'):
               ((digit>='a' && digit<='f')?(int)(digit-'a'+10):(int)(digit-'A'+10)));
         }
         // UTF-16 surrogate code units are valid only as an adjacent pair.
         if(code>=0xD800 && code<=0xDBFF)
         {
            if(at+6>length || StringGetCharacter(json,at++)!='\\' ||
               StringGetCharacter(json,at++)!='u') return false;
            int low=0;
            for(int j=0;j<4;j++)
            {
               ushort digit=StringGetCharacter(json,at++);
               if(!GSDL_IsHex(digit)) return false;
               low=low*16+(GSDL_IsDigit(digit)?(int)(digit-'0'):
                  ((digit>='a' && digit<='f')?(int)(digit-'a'+10):(int)(digit-'A'+10)));
            }
            if(low<0xDC00 || low>0xDFFF) return false;
            value+=ShortToString((ushort)code)+ShortToString((ushort)low);
         }
         else
         {
            if(code>=0xDC00 && code<=0xDFFF) return false;
            value+=ShortToString((ushort)code);
         }
      }
      else return false;
   }
   return false;
}

bool GSDL_ParseNumber(const string json,int &at,string &raw)
{
   int start=at,length=StringLen(json);
   if(at<length && StringGetCharacter(json,at)=='-') at++;
   if(at>=length) return false;
   ushort c=StringGetCharacter(json,at);
   if(c=='0')
   {
      at++;
      if(at<length && GSDL_IsDigit(StringGetCharacter(json,at))) return false;
   }
   else
   {
      if(c<'1' || c>'9') return false;
      while(at<length && GSDL_IsDigit(StringGetCharacter(json,at))) at++;
   }
   if(at<length && StringGetCharacter(json,at)=='.')
   {
      at++;
      if(at>=length || !GSDL_IsDigit(StringGetCharacter(json,at))) return false;
      while(at<length && GSDL_IsDigit(StringGetCharacter(json,at))) at++;
   }
   if(at<length && (StringGetCharacter(json,at)=='e' ||
      StringGetCharacter(json,at)=='E'))
   {
      at++;
      if(at<length && (StringGetCharacter(json,at)=='+' ||
         StringGetCharacter(json,at)=='-')) at++;
      if(at>=length || !GSDL_IsDigit(StringGetCharacter(json,at))) return false;
      while(at<length && GSDL_IsDigit(StringGetCharacter(json,at))) at++;
   }
   raw=StringSubstr(json,start,at-start);
   return MathIsValidNumber(StringToDouble(raw));
}

bool GSDL_IsPositiveInteger(const string raw)
{
   if(raw=="" || StringLen(raw)>20 || StringGetCharacter(raw,0)=='0') return false;
   for(int i=0;i<StringLen(raw);i++)
      if(!GSDL_IsDigit(StringGetCharacter(raw,i))) return false;
   if(StringLen(raw)==20 && StringCompare(raw,"18446744073709551615")>0)
      return false;
   return true;
}

bool GSDL_ParseUlong(const string raw,ulong &value)
{
   value=0;
   if(!GSDL_IsPositiveInteger(raw)) return false;
   for(int i=0;i<StringLen(raw);i++)
      value=value*10+(ulong)(StringGetCharacter(raw,i)-'0');
   return true;
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
   int at=0;
   GSDL_SkipSpace(json,at);
   if(at>=length || StringGetCharacter(json,at++)!='{') return false;
   string seen="|",event="",eventId="",tradeId="",account="",schema="",source="";
   string position="",deal="",order="";
   bool eventString=false,idString=false,tradeString=false;
   bool accountNumber=false,schemaNumber=false,sourceString=false;
   GSDL_SkipSpace(json,at);
   if(at<length && StringGetCharacter(json,at)=='}') return false;
   while(at<length)
   {
      string key="",value="";
      if(!GSDL_ParseString(json,at,key,true) || key=="" ||
         StringFind(seen,"|"+key+"|")>=0) return false;
      seen+=key+"|";
      GSDL_SkipSpace(json,at);
      if(at>=length || StringGetCharacter(json,at++)!=':') return false;
      GSDL_SkipSpace(json,at);
      if(at>=length) return false;
      ushort first=StringGetCharacter(json,at);
      bool stringValue=first=='"';
      bool numberValue=first=='-' || GSDL_IsDigit(first);
      if(first=='"')
      {
         if(!GSDL_ParseString(json,at,value,false)) return false;
      }
      else if(first=='n' && StringSubstr(json,at,4)=="null")
      { value="null"; at+=4; }
      else if(first=='t' && StringSubstr(json,at,4)=="true")
      { value="true"; at+=4; }
      else if(first=='f' && StringSubstr(json,at,5)=="false")
      { value="false"; at+=5; }
      else if(!GSDL_ParseNumber(json,at,value)) return false;
      if(key=="event_id") { eventId=value; idString=stringValue; }
      if(key=="event") { event=value; eventString=stringValue; }
      if(key=="trade_id") { tradeId=value; tradeString=stringValue; }
      if(key=="account_login") { account=value; accountNumber=numberValue; }
      if(key=="schema_version") { schema=value; schemaNumber=numberValue; }
      if(key=="source") { source=value; sourceString=stringValue; }
      if(key=="position_identifier") position=value;
      if(key=="deal_ticket") deal=value;
      if(key=="order_ticket") order=value;
      GSDL_SkipSpace(json,at);
      if(at>=length) return false;
      ushort delimiter=StringGetCharacter(json,at++);
      if(delimiter=='}') break;
      if(delimiter!=',') return false;
      GSDL_SkipSpace(json,at);
      if(at>=length || StringGetCharacter(json,at)=='}') return false;
   }
   GSDL_SkipSpace(json,at);
   if(at!=length || eventId=="" || event=="" || !eventString || !idString ||
      source=="" || !sourceString) return false;
   // Historical v1 rows have no account/trade/schema and are read-only.
   if(schema=="") return StringFind(eventId,"GSE2-")!=0;
   if(schema!="2" || !schemaNumber || tradeId=="" || !tradeString ||
      !accountNumber || !GSDL_IsPositiveInteger(account) || source!="MT5") return false;
   if(StringLen(account)>19 || (StringLen(account)==19 &&
      StringCompare(account,"9223372036854775807")>0)) return false;
   if(position!="" && position!="null" && !GSDL_IsPositiveInteger(position)) return false;
   if(deal!="" && deal!="null" && !GSDL_IsPositiveInteger(deal)) return false;
   if(order!="" && order!="null" && !GSDL_IsPositiveInteger(order)) return false;
   string kind=GSDL_CanonicalEvent(event);
   if(kind!="SIGNAL" && kind!="RESERVED" && kind!="ORDER_REQUESTED" &&
      kind!="ORDER_FILLED" && kind!="ORDER_REJECTED" &&
      kind!="POSITION_OPEN" && kind!="PARTIALLY_CLOSED" &&
      kind!="POSITION_CLOSED" && kind!="RECONCILED" &&
      kind!="RECONCILIATION_ERROR") return false;
   if(kind=="ORDER_FILLED" || kind=="PARTIALLY_CLOSED" ||
      kind=="POSITION_CLOSED")
      if(!GSDL_IsPositiveInteger(position) || !GSDL_IsPositiveInteger(deal)) return false;
   if(kind=="POSITION_OPEN" && !GSDL_IsPositiveInteger(position)) return false;
   return true;
}

struct GSDL_PositionState
{
   ulong positionId;
   string tradeId;
   string signalId;
   string attemptId;
   string status;
   bool closedSeen;
   bool reversalConflict;
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
   int riskFillCount;
   string excursionQuality;
};

struct GSDL_AttemptClaim
{
   string attemptId;
   string signalId;
   string symbol;
   long magic;
};

class GoldScoutDemoLedger
{
private:
   string m_filename;
   string m_ids[];
   int m_buckets[];
   string m_ambiguousTrades[];
   GSDL_AttemptClaim m_claims[];
   GSDL_PositionState m_positions[];
   bool m_healthy;
   int m_sequence;
   long m_account;

   bool RefreshAll(const int handle)
   {
      if(!FileSeek(handle,0,SEEK_SET)) return false;
      while(!FileIsEnding(handle))
      {
         string line=FileReadString(handle);
         StringTrimLeft(line);
         StringTrimRight(line);
         if(StringLen(line)>0 && StringGetCharacter(line,0)==65279)
            line=StringSubstr(line,1);
         if(line=="") continue;
         if(!GSDL_ValidJsonObject(line)) return false;
         string existingId=GSDL_JsonField(line,"event_id");
         if(!HasEvent(existingId)) Remember(line,existingId);
      }
      return true;
   }

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
      if(eventForOrder=="RESERVED")
      {
         string claimId=GSDL_JsonField(json,"execution_attempt_id");
         if(claimId!="" && claimId!="null")
         {
            int count=ArraySize(m_claims);
            ArrayResize(m_claims,count+1);
            m_claims[count].attemptId=claimId;
            m_claims[count].signalId=GSDL_JsonField(json,"signal_event_id");
            m_claims[count].symbol=GSDL_JsonField(json,"symbol");
            m_claims[count].magic=(long)StringToInteger(GSDL_JsonField(json,"magic"));
         }
      }
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
      ulong positionId=0;
      if(!GSDL_ParseUlong(GSDL_JsonField(json,"position_identifier"),positionId))
         GSDL_ParseUlong(GSDL_JsonField(json,"position_id"),positionId);
      if(positionId==0) return;
      int index=PositionIndex(positionId);
      if(index<0)
      {
         index=ArraySize(m_positions);
         ArrayResize(m_positions,index+1);
         m_positions[index].positionId=positionId;
         m_positions[index].status="UNKNOWN";
         m_positions[index].closedSeen=false;
         m_positions[index].reversalConflict=false;
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
         m_positions[index].riskFillCount=0;
         m_positions[index].excursionQuality="UNKNOWN";
      }
      string tradeId=GSDL_JsonField(json,"trade_id");
      string signalId=GSDL_JsonField(json,"signal_event_id");
      if(tradeId!="") m_positions[index].tradeId=tradeId;
      if(signalId!="") m_positions[index].signalId=signalId;
      string attemptId=GSDL_JsonField(json,"execution_attempt_id");
      if(attemptId!="" && attemptId!="null") m_positions[index].attemptId=attemptId;
      string setup=GSDL_JsonField(json,"setup");
      string tradeClass=GSDL_JsonField(json,"class");
      string direction=GSDL_JsonField(json,"direction");
      if(setup!="" && setup!="UNKNOWN") m_positions[index].setup=setup;
      if(tradeClass!="" && tradeClass!="UNKNOWN") m_positions[index].tradeClass=tradeClass;
      if(direction!="" && direction!="UNKNOWN") m_positions[index].direction=direction;
      string score=GSDL_JsonField(json,"score");
      if(score!="") m_positions[index].score=(int)StringToInteger(score);
      string kind=GSDL_CanonicalEvent(GSDL_JsonField(json,"event"));
      if(kind=="POSITION_OPEN" && !m_positions[index].reversalConflict &&
         !m_positions[index].closedSeen)
         m_positions[index].status="OPEN";
      if(kind=="POSITION_CLOSED")
      {
         m_positions[index].status=m_positions[index].reversalConflict?"ERROR":"CLOSED";
         m_positions[index].closedSeen=true;
      }
      if(kind=="RECONCILIATION_ERROR")
      {
         m_positions[index].status="ERROR";
         string reason=GSDL_JsonField(json,"reconciliation_status");
         if(StringFind(reason,"NETTING_INOUT_")==0 ||
            StringFind(reason,"HEDGING_INOUT_")==0 ||
            StringFind(reason,"UNKNOWN_MARGIN_MODE_INOUT_")==0)
            m_positions[index].reversalConflict=true;
      }
      if(kind=="RECONCILED" && !m_positions[index].closedSeen &&
         !m_positions[index].reversalConflict &&
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
            m_positions[index].riskFillCount++;
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
         m_positions[index].riskKnown=m_positions[index].fillCount>0 &&
            m_positions[index].fillCount==m_positions[index].riskFillCount;
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
   bool AttemptMatches(const string attemptId,const string signalId,
                       const string symbol,const long magic)
   {
      for(int i=0;i<ArraySize(m_claims);i++)
         if(m_claims[i].attemptId==attemptId && m_claims[i].signalId==signalId &&
            m_claims[i].symbol==symbol && m_claims[i].magic==magic)
            return true;
      return false;
   }
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
      ArrayResize(m_claims,0);
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
   bool ClaimAttempt(const string signalId,const string symbol,const long magic,
                     const string direction,const string setup,const string tradeClass,
                     const int score,string &attemptId)
   {
      attemptId="";
      if(!m_healthy || signalId=="") return false;
      int handle=FileOpen(m_filename,FILE_COMMON|FILE_READ|FILE_WRITE|FILE_TXT|
         FILE_ANSI|FILE_SHARE_READ,0,CP_UTF8);
      if(handle==INVALID_HANDLE) { m_healthy=false; return false; }
      if(!RefreshAll(handle)) { FileClose(handle); m_healthy=false; return false; }
      if(m_sequence>=2147483646)
      { FileClose(handle); m_healthy=false; return false; }
      int sequence=m_sequence+1;
      attemptId=StringFormat("GSA-%I64d-%d",m_account,sequence);
      string tradeId=GSDL_TradeId(m_account,symbol,magic,attemptId,0);
      string eventId="GSE2-"+attemptId+"|RESERVED";
      datetime now=TimeTradeServer();
      if(now<=0) now=TimeCurrent();
      string json=StringFormat("{\"event_id\":\"%s\",\"trade_id\":\"%s\",\"signal_event_id\":\"%s\",\"execution_attempt_id\":\"%s\",\"source\":\"MT5\",\"score_effect\":0,\"schema_version\":2,\"event\":\"RESERVED\",\"account_login\":%I64d,\"timestamp\":%I64d,\"sequence\":%d,\"symbol\":\"%s\",\"magic\":%I64d,\"direction\":\"%s\",\"setup\":\"%s\",\"class\":\"%s\",\"score\":%d}",
         eventId,tradeId,signalId,attemptId,m_account,(long)now,sequence,
         symbol,magic,direction,setup,tradeClass,score);
      if(!GSDL_ValidJsonObject(json) || !FileSeek(handle,0,SEEK_END))
      { FileClose(handle); m_healthy=false; attemptId=""; return false; }
      // A valid last JSON row may lack a trailing newline after a crash;
      // the leading separator keeps the next append from merging two rows.
      string record="\r\n"+json+"\r\n";
      uint written=FileWriteString(handle,record);
      FileFlush(handle);
      FileClose(handle);
      if(written<(uint)StringLen(record))
      { m_healthy=false; attemptId=""; return false; }
      Remember(json,eventId);
      return true;
   }
   bool Append(const string eventId,const string json)
   {
      if(!m_healthy || !GSDL_ValidJsonObject(json)) return false;
      if(HasEvent(eventId)) return true;
      int handle=FileOpen(m_filename,FILE_COMMON|FILE_READ|FILE_WRITE|FILE_TXT|
         FILE_ANSI|FILE_SHARE_READ,0,CP_UTF8);
      if(handle==INVALID_HANDLE) { m_healthy=false; return false; }
      string eventType=GSDL_CanonicalEvent(GSDL_JsonField(json,"event"));
      bool critical=eventType!="RECONCILED" ||
         GSDL_JsonField(json,"reconciliation_status")!="EXCURSION_SAMPLE";
      if(critical)
      {
         if(!RefreshAll(handle)) { FileClose(handle); m_healthy=false; return false; }
         if(HasEvent(eventId)) { FileClose(handle); return true; }
      }
      else
      {
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
      }
      if(!FileSeek(handle,0,SEEK_END)) { FileClose(handle); m_healthy=false; return false; }
      string record="\r\n"+json+"\r\n";
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
