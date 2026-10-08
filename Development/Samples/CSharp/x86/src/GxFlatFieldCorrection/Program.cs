using System;
using System.Collections.Generic;
using System.Linq;
using System.Text;
using GxFlatFieldCorrection;


namespace GxFlatFieldCorrection
{
    class Program
    {
        static void Main(string[] args)
        {
            CGxFlatFieldCorrection objFlatFieldCorrection = new CGxFlatFieldCorrection();
            objFlatFieldCorrection.FlatFieldCorrection();
        }
    }
}
