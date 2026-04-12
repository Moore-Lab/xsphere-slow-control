/*
********************************************************************************************************************************************
* This is a library for the ADS1115 A/D Converter
********************************************************************************************************************************************
*/
#include "Ads1115.h"

/*
********************************************************************************************************************************************
* Desc : This function initialises ADS1115 
* Ret  : 0 success
*        1 fail 
********************************************************************************************************************************************
*/
BOOL  Ads1115::Init (void)
{    
    INT08U  success; 
    BOOL    ret;

    I2cPtr->begin();
    I2cPtr->beginTransmission(I2cAddr);    
    success = I2cPtr->endTransmission();

    if (success == 0u) {
        WrReg(ADS1115_REG_CONFIG, ADS1115_RESET_VAL);        
        FullScaleRange = GetFullScaleRange();                                    /* default FSR                                            */
        ConvMode = GetConvMode();
        ret = true;
    } else {
        ret = false;
    }

    return (ret);
}

/*
********************************************************************************************************************************************
* Desc : This function resets the ADS1115 from the genteral call reset command (06)
********************************************************************************************************************************************
*/
void  Ads1115::Reset (void)
{
    I2cPtr->beginTransmission(0x00);                                             /* send general call address (0x00)                       */
    I2cPtr->write(0x06u);                                                        /* send general call reset command (0x06)                 */
    I2cPtr->endTransmission();                                                   /* end I2C communication                                  */
    
    return;
}

/*
********************************************************************************************************************************************
* Desc : This function sets a comparator que.
********************************************************************************************************************************************
*/
void  Ads1115::SetCompQue (T_ADS1115_COMP_QUE que)
{
    INT16U  cfg;

    cfg  = RdReg(ADS1115_REG_CONFIG);    
    cfg &= ~((1u << CFG_OS) | (3u << CFG_COMP_QUE0));     
    cfg |= que;
    WrReg(ADS1115_REG_CONFIG, cfg);
    
    return;
}

/*
********************************************************************************************************************************************
* Desc : This function sets a comparator latching.  
********************************************************************************************************************************************
*/
void  Ads1115::SetCompLat (T_ADS1115_COMP_LAT lat)
{
    INT16U  cfg; 
    
    cfg  = RdReg(ADS1115_REG_CONFIG);
    cfg &= ~((1 << CFG_OS) | (1 << CFG_COMP_LAT));
    cfg |= lat;
    WrReg(ADS1115_REG_CONFIG, cfg);
    
    return;
}

/*
**********************************************************************************************************************************************
* Desc : This function sets a comparator polarity.
**********************************************************************************************************************************************
*/
void  Ads1115::SetCompPol (T_ADS1115_COMP_POL pol)
{
    INT16U  cfg;
    
    cfg  = RdReg(ADS1115_REG_CONFIG);
    cfg &= ~((1 << CFG_OS) | (1 << CFG_COMP_POL));    
    cfg |= pol;
    WrReg(ADS1115_REG_CONFIG, cfg);
    
    return;
}

/*
********************************************************************************************************************************************
* Desc : This function sets comparator mode.
********************************************************************************************************************************************
*/
void  Ads1115::SetCompMode (T_ADS1115_COMP_MODE mode)
{
    INT16U  cfg;
    
    cfg  = RdReg(ADS1115_REG_CONFIG);
    cfg &= ~((1 << CFG_OS) | (1 << CFG_COMP_MODE));    
    cfg |= mode;
    WrReg(ADS1115_REG_CONFIG, cfg);
        
    return;
}

/*
********************************************************************************************************************************************
* Desc : This function sets thres voltages of high and low limits.
********************************************************************************************************************************************
*/
void  Ads1115::SetThreshVolt (FP32 hi_volt, FP32 lo_volt)
{
    INT16S  thresh; 
        
    thresh = VoltToRaw(hi_volt);    
    WrReg(ADS1115_REG_HI_THRESH, thresh);
    
    thresh = VoltToRaw(lo_volt);
    WrReg(ADS1115_REG_LO_THRESH, thresh);
    
    return;
}

/*
********************************************************************************************************************************************
* Desc : This function sets data rate.
********************************************************************************************************************************************
*/
void  Ads1115::SetDataRate (T_ADS1115_DR  dr)
{
    INT16U  cfg;

    cfg = RdReg(ADS1115_REG_CONFIG);           
    
    cfg &= ((1 << CFG_OS) | (7 << CFG_DR0));                            
    cfg |= dr;                               
    WrReg(ADS1115_REG_CONFIG, cfg);              
    
    return;
}

/*
********************************************************************************************************************************************
* Desc : This function returns data rate
********************************************************************************************************************************************
*/
T_ADS1115_DR  Ads1115::GetDataRate (void)
{
    INT16U        cfg;
    T_ADS1115_DR  dr;

    cfg = RdReg(ADS1115_REG_CONFIG);
    dr  = cfg & (7 << CFG_DR0);

    return (dr);
}

/*
********************************************************************************************************************************************
* Desc : This function sets conversion mode
********************************************************************************************************************************************
*/    
void  Ads1115::SetConvMode (T_ADS1115_MODE  mode)
{
    INT16U  cfg;
    
    cfg = RdReg(ADS1115_REG_CONFIG);    
    ConvMode = mode;
    cfg &= ~((1 << CFG_OS) | (1 << CFG_MODE));    
    cfg |= mode;
    WrReg(ADS1115_REG_CONFIG, cfg);
    
    return;
}

T_ADS1115_MODE  Ads1115::GetConvMode (void)
{
    INT16U  cfg;
    T_ADS1115_MODE  mode;
    
    cfg  = RdReg(ADS1115_REG_CONFIG);    
    mode = cfg & (1 << CFG_MODE);    
    
    return (mode);
}

/*
********************************************************************************************************************************************
* Desc : Set full scale range manually
********************************************************************************************************************************************
*/
void  Ads1115::SetFullScaleRange (T_ADS1115_PGA  pga)
{
    INT16S        thresh;
    FP32          scale;
    INT16U        full_scale_range;
    INT16U        cfg;
    T_ADS1115_DR  dr;
        
    SetConvMode(ADS1115_MODE_SINGLE);
    full_scale_range = FullScaleRange;
    
    switch(pga) {
        case ADS1115_PGA_6144:
            FullScaleRange = ADS1115_FSR_6144;           /* 6144 mV */
            break;
            
        case ADS1115_PGA_4096:
            FullScaleRange = ADS1115_FSR_4096;           /* 4096 mV */
            break;
            
        case ADS1115_PGA_2048:
            FullScaleRange = ADS1115_FSR_2048;           /* 2048 mV */
            break;
            
        case ADS1115_PGA_1024:
            FullScaleRange = ADS1115_FSR_1024;           /* 1024 mV */
            break;
            
        case ADS1115_PGA_0512:
            FullScaleRange = ADS1115_FSR_0512;            /* 512 mV */
            break;
            
        case ADS1115_PGA_0256:
            FullScaleRange = ADS1115_FSR_0256;            /* 256 mV */ 
            break;
            
        default:
            pga = ADS1115_PGA_2048;                      /* default */
            FullScaleRange = ADS1115_FSR_2048;           /* 2048 mV */
            break;    
    }

    cfg  = RdReg(ADS1115_REG_CONFIG);
    cfg &= ~((1 << CFG_OS) | (7 << CFG_PGA0));    
    cfg |= pga;
    WrReg(ADS1115_REG_CONFIG, cfg);

    scale  = (FP32)full_scale_range / FullScaleRange;
        
    thresh = RdReg(ADS1115_REG_HI_THRESH);
    thresh = thresh * scale;           
    WrReg(ADS1115_REG_HI_THRESH, thresh);
        
    thresh = RdReg(ADS1115_REG_LO_THRESH);
    thresh = thresh * scale;
    WrReg(ADS1115_REG_LO_THRESH, thresh);
    
    dr = GetDataRate();   
    DrDelay(dr);
    
    return;
}

/*
********************************************************************************************************************************************
* Desc : This function returns full scale range in mV
********************************************************************************************************************************************
*/
INT16S  Ads1115::GetFullScaleRange (void)
{
    INT16U cfg;
    INT16U pga;
    INT16S fsr; 

    cfg = RdReg(ADS1115_REG_CONFIG);
    pga = cfg & (7 << CFG_PGA0);

    switch(pga) {
        case ADS1115_PGA_6144:
            fsr = ADS1115_FSR_6144;           /* 6144 mV */
            break;
            
        case ADS1115_PGA_4096:
            fsr = ADS1115_FSR_4096;           /* 4096 mV */
            break;
            
        case ADS1115_PGA_2048:
            fsr = ADS1115_FSR_2048;           /* 2048 mV */
            break;
            
        case ADS1115_PGA_1024:
            fsr = ADS1115_FSR_1024;           /* 1024 mV */
            break;
            
        case ADS1115_PGA_0512:
            fsr = ADS1115_FSR_0512;            /* 512 mV */
            break;
            
        case ADS1115_PGA_0256:
            fsr = ADS1115_FSR_0256;            /* 256 mV */ 
            break;
            
        default:
            break;    
    }

    return (fsr);
}

/*
********************************************************************************************************************************************
* Desc : This function delay CPU accroding to data rate. 
********************************************************************************************************************************************
*/        
void  Ads1115::DrDelay(T_ADS1115_DR dr)
{
    switch(dr) {
        case ADS1115_SPS_8:
            delay(130);
            break;
            
        case ADS1115_SPS_16:
            delay(65);
            break;
            
        case ADS1115_SPS_32:
            delay(32);
            break;
            
        case ADS1115_SPS_64:
            delay(16);
            break;
            
        case ADS1115_SPS_128:
            delay(8);
            break;
            
        case ADS1115_SPS_250:
            delay(4);
            break;
            
        case ADS1115_SPS_475:
            delay(3);
            break;
            
        case ADS1115_SPS_860:
            delay(2);
            break;
    }
    
    return;
}
    
/*
******************************************************************************************************************************************
* Desc : This function selects a channel for measuring.
******************************************************************************************************************************************
*/
void  Ads1115::SetMux (T_ADS1115_MUX  mux)
{
    INT16U        cfg;
    T_ADS1115_DR  dr;    
    
    cfg  = RdReg(ADS1115_REG_CONFIG);
    cfg &= ~((1 << CFG_OS) | (7 << CFG_MUX0));    
    cfg |= mux;
    WrReg(ADS1115_REG_CONFIG, cfg);
    
    if (!(cfg & (1 << CFG_MODE))) {                           /* => if not single shot mode */
        dr = GetDataRate();      
        DrDelay(dr);
        DrDelay(dr);               
    } 

    return;    
}

/*
******************************************************************************************************************************************
* Desc : This function set a single-end channel from 0 to 3 
******************************************************************************************************************************************
*/
void  Ads1115::SetSingleCh (INT08U ch) 
{
    T_ADS1115_MUX  mux;
    
    if (ch < 4) {
        mux = ADS1115_MUX_SINGLE_0 | (0x1000u * ch); 
        SetMux(mux);
    }
    
    return;
}

/*
********************************************************************************************************************************************
* This function checks the ADS1115 status of operating
* 1: busy
* 0: free
********************************************************************************************************************************************
*/
BOOL  Ads1115::IsBusy (void)
{
    INT16U  cfg;
    BOOL    ret;

    cfg  = RdReg(ADS1115_REG_CONFIG);    
    cfg &= (1u << CFG_OS);

    if (cfg) {
        ret = false;        /* free */
    } else {
        ret = true;         /* busy */
    }
    
    return (ret);
}

/*
********************************************************************************************************************************************
* This function starts a single conversion.
********************************************************************************************************************************************
*/    
void  Ads1115::StartSingleConv (void)
{
    INT16U  cfg;

    cfg  = RdReg(ADS1115_REG_CONFIG);
    cfg |= (1 << CFG_OS);
    WrReg(ADS1115_REG_CONFIG, cfg);
    
    return;
}
 
/*
********************************************************************************************************************************************
* This function returns a resault in Volt
********************************************************************************************************************************************
*/ 
FP32  Ads1115::GetResultVolt (void)
{
    FP32  volt;
    
    volt  = GetResultMilliVolt();
    volt  = volt / 1000;
    
    return (volt);  
}

/*
********************************************************************************************************************************************
* This function returns a result in mV
********************************************************************************************************************************************
*/
FP32  Ads1115::GetResultMilliVolt (void)
{
    INT16S  raw;
    FP32    mv;
    
    raw = GetResultRaw();
    mv  = ((FP32)raw / 0x7FFF) * FullScaleRange;
    
    return (mv);
}

/*
********************************************************************************************************************************************
* Desc : This function returns a raw code from the ADS1115
********************************************************************************************************************************************
*/
INT16S  Ads1115::GetResultRaw (void)
{
    INT16S  raw;

    raw = RdReg(ADS1115_REG_CONV);
       
    return (raw);
}

/*
********************************************************************************************************************************************
* Desc : This function sets alert pin to be used as ready pin
********************************************************************************************************************************************
*/
void  Ads1115::SetAsReadyPin (void)
{
    WrReg(ADS1115_REG_LO_THRESH, (0 << 15u));
    WrReg(ADS1115_REG_HI_THRESH, (1 << 15u));
    
    return;
}

/*
********************************************************************************************************************************************
* Desc : This function clear the alert with reading converion data.
********************************************************************************************************************************************
*/
void  Ads1115::ClrAlert (void)
{
    RdReg(ADS1115_REG_CONV);
    
    return;
}

/*
********************************************************************************************************************************************
* Desc : This function convert voltage (not milivolt) to raw code.
********************************************************************************************************************************************
*/
INT16S  Ads1115::VoltToRaw (FP32 volt_limit)
{
    INT16S  raw_limit;
    
    raw_limit = static_cast<INT16S>((volt_limit * 10000 * 0x7FFF) / FullScaleRange);      /* ((volt * 1000) / FSR) * max_code. */
    
    return (raw_limit);
}

/*
********************************************************************************************************************************************
* Desc : This function writes a regiter
********************************************************************************************************************************************
*/
void  Ads1115::WrReg (INT08U reg, INT16U val)
{
    INT08U  lo;
    INT08U  hi;
    
    lo = val & 0xFF;
    hi = val >> 8;
    
    I2cPtr->beginTransmission(I2cAddr);
    I2cPtr->write(reg);
    I2cPtr->write(hi);
    I2cPtr->write(lo);
    I2cPtr->endTransmission();

    return; 
}

/*
********************************************************************************************************************************************
* Desc : This function reads a register
********************************************************************************************************************************************
*/  
INT16U  Ads1115::RdReg (INT08U reg) 
{
    INT08U  hi  = 0;
    INT08U  lo  = 0;
    INT16U  val = 0;
 
    I2cPtr->beginTransmission(I2cAddr);
    I2cPtr->write(reg);
    I2cPtr->endTransmission(false);
    I2cPtr->requestFrom(I2cAddr, (INT08U)2);
    
    if (I2cPtr->available()) {
        hi = I2cPtr->read();
        lo = I2cPtr->read();
        val = (hi << 8u) + lo;
    }
    
    return (val);
}

/*
********************************************************************************************************************************************
*                                                                     END OF FILE
********************************************************************************************************************************************
*/