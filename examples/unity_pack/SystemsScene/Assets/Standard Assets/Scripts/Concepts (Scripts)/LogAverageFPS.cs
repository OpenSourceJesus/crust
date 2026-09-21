using System.IO;
using UnityEngine;

public class LogAverageFPS : MonoBehaviour
{
	const float TIME = 1;
	static string LOG_FILE_PATH = Application.persistentDataPath + "/AverageFPS.txt";
	float timeLeft;
	int frameCnt;

	void Start ()
	{
		timeLeft = TIME;
	}

	void Update ()
	{
		timeLeft -= Time.deltaTime;
		frameCnt ++;
		if (timeLeft <= 0)
		{
			File.AppendAllText(LOG_FILE_PATH, "Average FPS: " + (frameCnt / (TIME - timeLeft)) + '\n');
			Destroy(gameObject);
		}
	}
}
