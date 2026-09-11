#ifndef GOLDSCOUT_TRADE_COMPILE_STUB_MQH
#define GOLDSCOUT_TRADE_COMPILE_STUB_MQH

// Compile-only surface for environments where MetaEditor's standard library
// is not installed alongside the compiler. Never deploy this file to MT5.
class CTrade
{
public:
   void SetExpertMagicNumber(const ulong magic) {}
   void SetDeviationInPoints(const ulong deviation) {}
   bool PositionOpen(const string symbol,const ENUM_ORDER_TYPE orderType,
                     const double volume,const double price,const double sl,
                     const double tp,const string comment="") { return false; }
   uint ResultRetcode() const { return 0; }
   ulong ResultDeal() const { return 0; }
   double ResultPrice() const { return 0.0; }
};

#endif
