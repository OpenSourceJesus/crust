using System.IO;
using UnityEngine;

public class LogAverageFPS : MonoBehaviour
{
	const short FRAME_CNT = 100;
	static string LOG_FILE_PATH = Application.dataPath + "/Logs/AverageFPS.txt";
	short framesLeft;

	void Start ()
	{
		framesLeft = FRAME_CNT;
	}

	void Update ()
	{
		framesLeft --;
		if (framesLeft == 0)
		{
			File.AppendAllText(LOG_FILE_PATH, "Average FPS: " + (FRAME_CNT / Time.time) + '\n');
			Destroy(gameObject);
		}
	}
}
