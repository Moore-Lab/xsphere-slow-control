/******************************************************************************
*
* This is a library for the ADS1115 A/D Converter
*
*******************************************************************************
*/

#ifndef ADS1115_H
#define ADS1115_H

#include <Arduino.h>
#include <Wire.h>


#define  CFG_OS                       (15u)
#define  CFG_MUX2                     (14u)
#define  CFG_MUX1                     (13u)
#define  CFG_MUX0                     (12u)
#define  CFG_PGA2                     (11u)
#define  CFG_PGA1                     (10u) 
#define  CFG_PGA0                      (9u)
#define  CFG_MODE                      (8u)
#define  CFG_DR2                       (7u)
#define  CFG_DR1                       (6u) 
#define  CFG_DR0                       (5u)
#define  CFG_COMP_MODE                 (4u)
#define  CFG_COMP_POL                  (3u)
#define  CFG_COMP_LAT                  (2u)
#define  CFG_COMP_QUE1                 (1u) 
#define  CFG_COMP_QUE0                 (0u)    
 
typedef  bool            BOOL; 
typedef  int             INT16S;
typedef  unsigned int    INT16U;
typedef  char            INT08S;
typedef  unsigned char   INT08U;
typedef  float           FP32; 

typedef  INT16U    T_ADS1115_COMP_QUE;
#define  ADS1115_QUE_AFTER_1            (0x0000u)
#define  ADS1115_QUE_AFTER_2            (0x0001u)
#define  ADS1115_QUE_AFTER_4            (0x0002u)
#define  ADS1115_QUE_DISABLE            (0x0003u)

typedef  INT16U    T_ADS1115_COMP_LAT;
#define  ADS1115_NONLATCHING            (0x0000u)
#define  ADS1115_LATCHING               (0x0004u)

typedef  INT16U    T_ADS1115_COMP_POL; 
#define  ADS1115_POL_LOW                (0x0000u)
#define  ADS1115_POL_HIGH               (0x0008u)

typedef  INT16U    T_ADS1115_COMP_MODE; 
#define  ADS1115_COMP_TRAD              (0x0000u)
#define  ADS1115_COMP_WINDOW            (0x0010u)

typedef  INT16U    T_ADS1115_DR; 
#define  ADS1115_SPS_8                  (0x0000u)
#define  ADS1115_SPS_16                 (0x0020u)
#define  ADS1115_SPS_32                 (0x0040u)
#define  ADS1115_SPS_64                 (0x0060u)
#define  ADS1115_SPS_128                (0x0080u)
#define  ADS1115_SPS_250                (0x00A0u)
#define  ADS1115_SPS_475                (0x00C0u)
#define  ADS1115_SPS_860                (0x00E0u)

typedef  INT16U     T_ADS1115_MODE; 
#define  ADS1115_MODE_CONTINUOUS        (0x0000u) 
#define  ADS1115_MODE_SINGLE            (0x0100u)

typedef  INT16U     T_ADS1115_PGA; 
#define  ADS1115_FSR_6144               (6144)
#define  ADS1115_FSR_4096               (4096)
#define  ADS1115_FSR_2048               (2048)
#define  ADS1115_FSR_1024               (1024)
#define  ADS1115_FSR_0512               (512)
#define  ADS1115_FSR_0256               (256)

#define  ADS1115_PGA_6144               (0x0000u)
#define  ADS1115_PGA_4096               (0x0200u)
#define  ADS1115_PGA_2048               (0x0400u)
#define  ADS1115_PGA_1024               (0x0600u)
#define  ADS1115_PGA_0512               (0x0800u)
#define  ADS1115_PGA_0256               (0x0A00u)

typedef  INT16U     T_ADS1115_MUX; 
#define  ADS1115_MUX_DIFF_0_1           (0x0000u)
#define  ADS1115_MUX_DIFF_0_3           (0x1000u)
#define  ADS1115_MUX_DIFF_1_3           (0x2000u)
#define  ADS1115_MUX_DIFF_2_3           (0x3000u)
#define  ADS1115_MUX_SINGLE_0           (0x4000u)
#define  ADS1115_MUX_SINGLE_1           (0x5000u)
#define  ADS1115_MUX_SINGLE_2           (0x6000u)
#define  ADS1115_MUX_SINGLE_3           (0x7000u)

typedef  INT16U  T_ADS1115_OS; 
#define  ADS1115_OS_BUSY                (0x0000u)
#define  ADS1115_OS_READY               (0x8000u)

#define  ADS1115_RESET_VAL              (0x8583u)

/* registers */
#define  ADS1115_REG_CONV               (0x00u)       /* Conversion Register */
#define  ADS1115_REG_CONFIG             (0x01u)       /* Configuration Register  */
#define  ADS1115_REG_LO_THRESH          (0x02u)       /* Low Threshold Register */
#define  ADS1115_REG_HI_THRESH          (0x03u)       /* High Threshold Register */

class Ads1115
{
    public:
		Ads1115(const INT08U addr = 0x48) : I2cPtr{&Wire}, I2cAddr{addr} {}

        BOOL            Init(void);
        void            Reset(void);
        void            SetCompQue(T_ADS1115_COMP_QUE que);
        void            SetCompLat(T_ADS1115_COMP_LAT lat);
        void            SetCompPol(T_ADS1115_COMP_POL pol);
        void            SetCompMode(T_ADS1115_COMP_MODE mode);
        void            SetThreshVolt(FP32 hi_volt, FP32 lo_volt);
        void            SetDataRate(T_ADS1115_DR  dr);
        T_ADS1115_DR    GetDataRate(void);
        void            SetConvMode(T_ADS1115_MODE mode);
        T_ADS1115_MODE  GetConvMode (void);
        void            SetFullScaleRange(T_ADS1115_PGA  new_pga);
        void            SetMux(T_ADS1115_MUX  mux);
        void            SetSingleCh(INT08U ch); 
        BOOL            IsBusy(void);
        void            StartSingleConv(void);
        FP32            GetResultVolt(void);
        FP32            GetResultMilliVolt(void);
        INT16S          GetResultRaw(void);
        INT16S          GetFullScaleRange(void);
        void            SetAsReadyPin(void);
        void            ClrAlert(void);

    private:
        TwoWire         *I2cPtr;
        INT16S          FullScaleRange;
        T_ADS1115_MODE  ConvMode;
        INT08U          I2cAddr;
        
        void            DrDelay(T_ADS1115_DR dr);
        INT16S          VoltToRaw(FP32 volt);
        void            WrReg(INT08U reg, INT16U val);
        INT16U          RdReg(INT08U reg);
    };

#endif

